"""Human-readable explanations from the disposable state index."""

from __future__ import annotations

import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from team_harness.state_index import update
from team_harness.workspace_archive import watch_dir

# A successful later attempt resolves an earlier failure even if the task snapshot lags.
UNRESOLVED = """t.state IN ('failed','verify_failed') AND NOT EXISTS (
 SELECT 1 FROM attempts a WHERE a.task=t.task AND a.attempt>f.attempt
 AND lower(a.result) IN ('done','verified'))"""


def failure_signature(detail: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return normalized failing checks and causes, excluding diagnostic noise."""
    checks = tuple(
        sorted(set(re.findall(r"(?m)^\s*-?\s*([^\n:]+):\s*\[FAIL\]", detail)))
    )
    checks = tuple(x.strip() for x in checks)
    causes = []
    for pattern, label in (
        (r"STOPPED_BY_USER", "STOPPED_BY_USER"),
        (r"\b429\b|rate[ -]?limit|quota|쿼터", "quota"),
        (r"\bQUEUED\b|프로세스 소실", "process_lost"),
        (r"\bauth(?:entication|orization)?\b|unauthorized|forbidden|인증", "auth"),
    ):
        if re.search(pattern, detail, re.I):
            causes.append(label)
    step = re.search(r"\bstep=([^\s,]+).*?\brc=(\d+)", detail)
    if step:
        causes.append(f"{step.group(1)} rc={step.group(2)}")
    for line in detail.splitlines():
        if re.search(r"error|exception|오류|실패", line, re.I) and not re.search(
            r"\[(?:SKIP|FAIL)\]|Traceback", line, re.I
        ):
            line = re.sub(r"(?:/[^\s:]+)+", "<path>", line.strip())
            line = re.sub(r"\b[0-9a-f]{8,}\b|\d+", "#", line, flags=re.I)
            causes.append(line.lower()[:120])
            break
    if not checks and not causes and len(detail.splitlines()) == 1:
        line = re.sub(r"(?:/[^\s:]+)+", "<path>", detail.strip())
        line = re.sub(r"\b[0-9a-f]{8,}\b|\d+", "#", line, flags=re.I)
        if line:
            causes.append(line.lower()[:120])
    return checks, tuple(sorted(causes))


def _failure_summary(category: str, detail: str) -> str:
    checks, causes = failure_signature(detail or "")
    parts = [category, *(causes or ()), *(f"FAIL {item}" for item in checks)]
    return " · ".join(parts)[:200]


def normalize_workspace(workspace: str) -> str:
    path = Path(workspace).expanduser().resolve()
    try:
        common = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            timeout=1,
            check=True,
        ).stdout.strip()
        git_dir = Path(common)
        if not git_dir.is_absolute():
            git_dir = path / git_dir
        return str(git_dir.resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return str(path)


def resolve_target(db: sqlite3.Connection, target: str) -> str | None:
    if target.isdigit():
        row = db.execute(
            "SELECT task FROM processes WHERE pid=? ORDER BY last_seen DESC LIMIT 1",
            (int(target),),
        ).fetchone()
    else:
        path = str(Path(target).expanduser().resolve())
        row = db.execute(
            "SELECT task FROM tasks WHERE task=? OR task_key=? ORDER BY updated_at DESC LIMIT 1",
            (path, target),
        ).fetchone()
    return str(row[0]) if row else None


def launch_recall(target: str, task_key: str = "") -> str:
    if os.environ.get("TH_AUTO_RECALL") == "0":
        return ""
    path = watch_dir() / "th.db"
    if not path.exists():
        return ""
    with sqlite3.connect(
        f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1
    ) as db:
        tasks = db.execute(
            "SELECT task FROM tasks WHERE task=? OR (task_key != '' AND task_key=?)",
            (str(Path(target).expanduser().resolve()), task_key or Path(target).name),
        ).fetchall()
        lines = []
        for (task,) in tasks:
            (count,) = db.execute(
                "SELECT count(*) FROM attempts WHERE task=?", (task,)
            ).fetchone()
            if not count:
                continue
            last = db.execute(
                "SELECT result FROM attempts WHERE task=? ORDER BY attempt DESC LIMIT 1",
                (task,),
            ).fetchone()
            failure = db.execute(
                "SELECT category,detail FROM failures WHERE task=? ORDER BY attempt DESC, at DESC LIMIT 1",
                (task,),
            ).fetchone()
            lines.append(
                f"이전 작업: {task} · 시도 {count}회 · 마지막 결과 {last[0] or '미상'}"
            )
            if failure:
                lines.append(
                    f"실패 원인: {failure[0]} · {' '.join((failure[1] or '').splitlines())}"
                )
        return "\n".join(lines[:6])


def connect() -> sqlite3.Connection:
    path = watch_dir() / "th.db"
    if not path.exists() or time.time() - path.stat().st_mtime > 30:
        update()
    db = sqlite3.connect(path, timeout=1)
    present = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='processes'"
    ).fetchone()
    if not present:
        db.close()
        update()
        db = sqlite3.connect(path, timeout=1)
    return db


def why(target: str, limit: int = 30, *, readonly: bool = False) -> str:
    path = watch_dir() / "th.db"
    connection = (
        sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1)
        if readonly
        else connect()
    )
    with connection as db:
        task = resolve_target(db, target)
        if not task:
            return "작업을 찾지 못했습니다."
        lines = [f"작업: {task}"]
        row = db.execute(
            "SELECT state,last_step FROM tasks WHERE task=?", (task,)
        ).fetchone()
        if row:
            lines.append(f"상태: {row[0]} · 마지막 단계: {row[1] or '없음'}")
        processes = db.execute(
            "SELECT pid,ended_at,origin,session_id,workspace,worker_task FROM processes WHERE task=? ORDER BY last_seen DESC LIMIT 3",
            (task,),
        ).fetchall()
        for pid, ended, origin, session, workspace, worker in processes:
            lines.append(
                f"프로세스: {pid} {'종료' if ended else '관찰 중'} · 출처 {origin} · 세션 {session or '없음'} · 워커 {worker or '없음'} · {workspace or ''}"
            )
        for model, result, tokens in db.execute(
            "SELECT model,result,tokens FROM attempts WHERE task=? ORDER BY attempt DESC LIMIT 3",
            (task,),
        ):
            lines.append(
                f"시도: {model or '미상'} · {result or '미상'} · 토큰 {tokens if tokens is not None else '미상'}"
            )
        failure = db.execute(
            "SELECT category,detail FROM failures WHERE task=? ORDER BY at DESC LIMIT 1",
            (task,),
        ).fetchone()
        if failure:
            unresolved = db.execute(
                f"SELECT 1 FROM failures f JOIN tasks t ON t.task=f.task WHERE f.task=? AND {UNRESOLVED} LIMIT 1",
                (task,),
            ).fetchone()
            summary = _failure_summary(failure[0], failure[1])
            if not unresolved:
                lines.append(f"지난 실패(해결됨): {summary}")
            else:
                lines.append(f"실패: {summary}")
                signature = failure_signature(failure[1] or "")
                owner, key = db.execute(
                    "SELECT owner,task_key FROM tasks WHERE task=?", (task,)
                ).fetchone()
                prefix = (key or "").split("_")[0]
                candidates = db.execute(
                    f"SELECT f.task,f.detail,t.owner,t.task_key FROM failures f JOIN tasks t ON t.task=f.task WHERE f.category=? AND f.task!=? AND datetime(f.at)>=datetime('now','-7 days') AND {UNRESOLVED} ORDER BY f.at DESC",
                    (failure[0], task),
                ).fetchall()
                matches = []
                seen = set()
                for other, detail, other_owner, other_key in candidates:
                    if other in seen:
                        continue
                    seen.add(other)
                    if (
                        signature != ((), ())
                        and failure_signature(detail or "") == signature
                    ):
                        priority = bool(owner and owner == other_owner) or bool(
                            prefix and (other_key or "").split("_")[0] == prefix
                        )
                        matches.append((not priority, other, detail))
                matches.sort(key=lambda item: item[0])
                if not matches:
                    lines.append("유사 실패 없음")
                for _, other, detail in matches[:3]:
                    lines.append(
                        f"유사 실패: {other} · {_failure_summary(failure[0], detail)}"
                    )
                    for (text,) in db.execute(
                        "SELECT text FROM coord_log WHERE task=? AND kind IN ('진단','결정','실패') ORDER BY at DESC LIMIT 1",
                        (other,),
                    ):
                        lines.append(f"  관련 기록: {text}")
        for (text,) in db.execute(
            "SELECT text FROM events WHERE task=? OR text LIKE ? ORDER BY at DESC LIMIT 3",
            (task, f"%{Path(task).name}%"),
        ):
            lines.append(f"사건: {text}")
        for (text,) in db.execute(
            "SELECT text FROM coord_log WHERE task=? ORDER BY at DESC LIMIT 3", (task,)
        ):
            lines.append(f"코디네이터: {text}")
        for name in ("worker.log", "codex.log", "claude.log", "agy.log"):
            try:
                with (Path(task) / name).open("rb") as stream:
                    stream.seek(0, os.SEEK_END)
                    stream.seek(max(0, stream.tell() - 65536))
                    tail = stream.read().decode(errors="replace").splitlines()[-15:]
            except OSError:
                continue
            lines.extend(
                f"로그: {line[:300]}"
                for line in tail
                if re.search(r"error|fail|exception|오류|실패", line, re.I)
            )
        return "\n".join(lines[:limit])


def main() -> None:
    if (
        os.environ.get("TH_AUTO_RECALL") == "0"
        and len(sys.argv) > 1
        and sys.argv[1] == "recent"
    ):
        return
    if len(sys.argv) > 1 and sys.argv[1] == "recent":
        workspace = normalize_workspace(sys.argv[2])
        with connect() as db:
            rows = db.execute(
                "SELECT text FROM coord_log WHERE workspace=? AND kind='결정' AND datetime(at)>=datetime('now','-1 day') ORDER BY at DESC LIMIT 5",
                (workspace,),
            ).fetchall()
            failures = db.execute(
                f"SELECT t.task,f.category FROM failures f JOIN tasks t ON t.task=f.task WHERE t.owner=? AND datetime(f.at)>=datetime('now','-1 day') AND {UNRESOLVED} GROUP BY t.task ORDER BY f.at DESC LIMIT 4",
                (workspace,),
            ).fetchall()
        if rows or failures:
            print("## 미해결 실패·최근 결정")
            for task, category in failures:
                print(f"실패: {task} · {category}")
            for (text,) in rows:
                print(f"결정: {text}")
    else:
        print(why(sys.argv[1]))


if __name__ == "__main__":
    main()
