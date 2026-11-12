"""Best-effort notifications and observation-only session breakers."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess

from team_harness import usage_ledger
from team_harness.workspace_archive import watch_dir


def coordinator_workspace(
    workspace: str, pid: int | str | None = None, session_pid: int | str | None = None
) -> str:
    """Resolve the observed session owner without changing its stored workspace."""
    for candidate in (pid, session_pid):
        if not candidate:
            continue
        try:
            entries = (
                (Path("/proc") / str(int(candidate)) / "environ")
                .read_bytes()
                .split(b"\0")
            )
            for entry in entries:
                if entry.startswith(b"TH_WORKSPACE="):
                    value = os.fsdecode(entry.partition(b"=")[2])
                    if value and Path(value).is_absolute():
                        return value
        except (OSError, ValueError):
            pass
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                workspace,
                "rev-parse",
                "--is-inside-work-tree",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        lines = result.stdout.splitlines()
        if len(lines) == 2 and lines[0] == "true":
            common = Path(lines[1])
            if common.is_absolute():
                return str(common.parent)
    except (OSError, subprocess.SubprocessError):
        pass
    return workspace


def _append(workspace: str, session: str, message: str) -> None:
    owner = Path(workspace)
    if not owner.is_absolute() or not owner.is_dir():
        return
    directory = owner / ".claude"
    if not directory.is_dir():
        return
    route = (
        session
        if session
        and not any(c in session for c in "/\t\n")
        and session not in {".", "..", "*"}
        else "*"
    )
    with (directory / ".inbox_cursor.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        inbox = directory / "inbox.md"
        with inbox.open("ab") as out:
            offset = out.tell()
            with (directory / "inbox.routes").open("a", encoding="utf-8") as routes:
                routes.write(f"{offset}\t{route}\n")
            out.write((message.replace("\n", " ") + "\n").encode())
            out.flush()
            unread = watch_dir() / "inbox_unread"
            unread.mkdir(parents=True, exist_ok=True)
            key = hashlib.sha256(workspace.encode()).hexdigest()
            with (unread / f"{key}.tsv").open("a", encoding="utf-8") as state:
                state.write(
                    f"{int(datetime.now(UTC).timestamp())}\t{out.tell()}\t0\t{workspace}\t{route}\tunknown\n"
                )


def _summary(task: str, command: str) -> str:
    if task:
        try:
            lines = (
                (Path(task) / "task.md")
                .read_text(encoding="utf-8", errors="replace")
                .splitlines()
            )
            heading = next(
                (line.lstrip("# ").strip() for line in lines if line.startswith("# ")),
                "",
            )
            if heading:
                return heading[:160]
        except OSError:
            pass
    return Path(task).name if task else (command or "관찰 작업")


def _result(task: str) -> str:
    if task:
        for name in ("FAILED", "DONE", "STATUS"):
            try:
                lines = (
                    (Path(task) / name)
                    .read_text(encoding="utf-8", errors="replace")
                    .splitlines()
                )
                if lines:
                    return next(
                        (line.strip() for line in reversed(lines) if line.strip()),
                        "결과 표식 없음",
                    )[:300]
            except OSError:
                continue
    return "결과 표식 없음"


def _threshold(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value)
        return (
            result.astimezone(UTC)
            if result.tzinfo
            else result.astimezone().astimezone(UTC)
        )
    except ValueError:
        result = usage_ledger.parse_time(value)
        return result.astimezone(UTC) if result else None


def process_events(db: sqlite3.Connection, registered: set[Path], now: str) -> None:
    """Called only by maintenance, after processes and attempts have been indexed."""
    now_dt = datetime.fromisoformat(now)
    cutoff = (now_dt - timedelta(hours=1)).isoformat()
    if os.environ.get("TH_OBSERVED_NOTIFY") != "0":
        rows = db.execute(
            "SELECT pid,start,session_id,workspace,worker_task,task,cmd, (SELECT claude_pid FROM sessions WHERE sessions.session_id=processes.session_id) FROM processes WHERE ended_at IS NOT NULL AND origin IN ('worker','coordinator')"
        ).fetchall()
        for pid, start, session, workspace, worker_task, task, cmd, claude_pid in rows:
            location = task or worker_task or ""
            if not workspace or (location and Path(location) in registered):
                continue
            identity = "task:" + location if location else f"pid:{pid}:{start}"
            if db.execute(
                "SELECT 1 FROM observed_notices WHERE identity=?", (identity,)
            ).fetchone():
                continue
            if (
                location
                and db.execute(
                    "SELECT 1 FROM processes WHERE ended_at IS NULL AND (task=? OR worker_task=?)",
                    (location, location),
                ).fetchone()
            ):
                continue
            summary = _summary(location, cmd or "")
            command = cmd or ""
            label = command[:60] or (
                os.path.relpath(location, workspace) if location else "관찰 작업"
            )
            detail = f"{summary} — " if summary and summary != command else ""
            _append(
                coordinator_workspace(workspace, pid, claude_pid),
                session or "",
                f"{now_dt.astimezone().strftime('%Y-%m-%d %H:%M')} | 관찰:{label} | 관찰 종료 | {detail}{_result(location)} | -",
            )
            db.execute(
                "INSERT OR IGNORE INTO observed_notices VALUES (?,?)", (identity, now)
            )

    sessions = db.execute(
        "SELECT DISTINCT session_id,workspace FROM processes WHERE session_id IS NOT NULL AND workspace IS NOT NULL UNION SELECT DISTINCT session_id,workspace FROM shim_decisions WHERE session_id IS NOT NULL AND workspace IS NOT NULL"
    ).fetchall()
    for session, workspace in sessions:
        tasks = {
            row[0]
            for row in db.execute(
                "SELECT task FROM processes WHERE session_id=? AND task IS NOT NULL UNION SELECT worker_task FROM processes WHERE session_id=? AND worker_task IS NOT NULL UNION SELECT worker_task FROM shim_decisions WHERE session_id=? AND worker_task IS NOT NULL",
                (session, session, session),
            )
            if row[0]
        }
        attempts = []
        for task in tasks:
            attempts.extend(
                db.execute(
                    "SELECT task,attempt,started_at,tokens,result FROM attempts WHERE task=?",
                    (task,),
                ).fetchall()
            )
        attempts.sort(
            key=lambda row: _stamp(row[2]) or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        )
        tokens = sum(
            row[3] or 0
            for row in attempts
            if (stamp := _stamp(row[2])) and stamp >= now_dt - timedelta(hours=1)
        )
        recent = attempts[:10]
        retries = sum(
            1
            for row in recent
            if row[1] > 1
            or "fail" in (row[4] or "").lower()
            or "실패" in (row[4] or "")
        )
        triggers: list[tuple[str, str]] = []
        if tokens >= _threshold("TH_BREAKER_TOKENS", 300000):
            triggers.append(("tokens", f"최근 1시간 토큰 {tokens}"))
        if len(recent) >= 10 and retries >= _threshold("TH_BREAKER_RETRIES", 6):
            triggers.append(("retries", f"최근 10회 중 재실행·실패 {retries}회"))
        groups: dict[tuple[str, str, str], set[tuple[str, int]]] = {}
        for task, verdicts in db.execute(
            "SELECT worker_task,verdicts_json FROM shim_decisions WHERE session_id=? AND worker_task IS NOT NULL",
            (session,),
        ):
            try:
                data = json.loads(verdicts)
                location, prompt = data.get("work_location"), data.get("prompt_hash")
                if not location or not prompt:
                    continue
                for attempt, category in db.execute(
                    "SELECT attempt,category FROM failures WHERE task=?", (task,)
                ):
                    groups.setdefault((location, prompt, category), set()).add(
                        (task, attempt)
                    )
            except (ValueError, TypeError, AttributeError):
                continue
        for (location, prompt, category), failures in groups.items():
            if len(failures) >= _threshold("TH_BREAKER_FAILURES", 2):
                triggers.append(
                    (
                        f"failure:{location}:{prompt}:{category}",
                        f"같은 원인 반복 실패 {len(failures)}회 ({category}, {location})",
                    )
                )
        for reason, value in triggers:
            if db.execute(
                "SELECT 1 FROM breaker_events WHERE session_id=? AND reason=? AND at>=?",
                (session, reason, cutoff),
            ).fetchone():
                continue
            db.execute(
                "INSERT INTO breaker_events VALUES (?,?,?,?,?)",
                (now, session, workspace, reason, value),
            )
            _append(coordinator_workspace(workspace), session, f"차단기(관찰): {value}")
