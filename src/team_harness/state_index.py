"""Disposable, incrementally updated SQLite copy of file-backed state."""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys

from team_harness import loss_report
from team_harness import proc_observer
from team_harness import usage_ledger
from team_harness.workspace_archive import watch_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS shim_decisions(at TEXT, pid INTEGER, session_id TEXT, workspace TEXT, worker_task TEXT, tool TEXT, model TEXT, effort TEXT, depth INTEGER, verdicts_json TEXT, would_block INTEGER, mode TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tasks(task TEXT PRIMARY KEY, owner TEXT, task_key TEXT, kind TEXT, first_seen TEXT, state TEXT, last_step TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS attempts(task TEXT, attempt INTEGER, worker TEXT, model TEXT, effort TEXT, started_at TEXT, minutes REAL, tokens INTEGER, result TEXT, override TEXT, PRIMARY KEY(task, attempt));
CREATE TABLE IF NOT EXISTS failures(task TEXT, attempt INTEGER, at TEXT, category TEXT, detail TEXT, source TEXT, UNIQUE(task, attempt, source, detail));
CREATE TABLE IF NOT EXISTS events(source TEXT, line_hash TEXT UNIQUE, at TEXT, task TEXT, kind TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS adoptions(folder TEXT, verifier TEXT, claim TEXT, verdict TEXT, counterexample TEXT, at TEXT, UNIQUE(folder, verifier, at));
CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, claude_pid TEXT, workspace TEXT, model TEXT, first_seen TEXT, last_seen TEXT);
CREATE TABLE IF NOT EXISTS processes(pid INTEGER, start TEXT, first_seen TEXT, last_seen TEXT, ended_at TEXT, kind TEXT, origin TEXT, session_id TEXT, workspace TEXT, worker_task TEXT, task TEXT, cmd TEXT, PRIMARY KEY(pid, start));
CREATE TABLE IF NOT EXISTS observed_notices(identity TEXT PRIMARY KEY, at TEXT);
CREATE TABLE IF NOT EXISTS breaker_events(at TEXT, session_id TEXT, workspace TEXT, reason TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS coord_log(workspace TEXT, at TEXT, kind TEXT, task TEXT, text TEXT, line_hash TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS ingest_state(source TEXT PRIMARY KEY, inode INTEGER, offset INTEGER);
"""


def _warn(path: Path, exc: Exception) -> None:
    print(f"state index: {path}: {exc}", file=sys.stderr)


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        _warn(path, exc)
        return ""


def _time(value: str) -> datetime | None:
    match = usage_ledger.STATUS_TIME_RE.match(value.strip())
    return usage_ledger.parse_time(match.group(1)) if match else None


def _lines(db: sqlite3.Connection, path: Path, start: datetime):
    try:
        with path.open("rb") as stream:
            stat = path.stat()
            row = db.execute(
                "SELECT inode, offset FROM ingest_state WHERE source=?", (str(path),)
            ).fetchone()
            offset = (
                row[1]
                if row and row[0] == stat.st_ino and row[1] <= stat.st_size
                else 0
            )
            stream.seek(offset)
            while True:
                pos = stream.tell()
                raw = stream.readline()
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    stream.seek(pos)
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                stamp = _time(line)
                if stamp and stamp >= start:
                    yield line, stamp
            db.execute(
                "INSERT OR REPLACE INTO ingest_state VALUES (?,?,?)",
                (str(path), stat.st_ino, stream.tell()),
            )
    except FileNotFoundError:
        return
    except OSError as exc:
        _warn(path, exc)


def _paths(wd: Path) -> set[Path]:
    tasks = {
        Path(line.strip()).expanduser().resolve()
        for name in ("tasks.txt", "tasks_done.txt")
        for line in _read(wd / name).splitlines()
        if line.strip()
    }
    try:
        admission = json.loads(_read(wd / "admission.json") or "{}")
        tasks.update(
            Path(row["task"]).expanduser().resolve()
            for row in admission.values()
            if isinstance(row, dict) and row.get("task")
        )
    except (ValueError, AttributeError, TypeError) as exc:
        _warn(wd / "admission.json", exc)
    return tasks


def _modified(path: Path, start: datetime) -> bool:
    try:
        return path.stat().st_mtime >= start.timestamp()
    except OSError:
        return False


def update(
    observations: list[dict] | None = None, *, maintenance: bool = False
) -> None:
    wd = watch_dir()
    wd.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(wd / "th.db", timeout=10) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=10000")
        db.executescript(SCHEMA)
        now = datetime.now(UTC).isoformat()
        db.execute("INSERT OR REPLACE INTO meta VALUES ('schema_version','2')")
        db.execute("INSERT OR IGNORE INTO meta VALUES ('started_at',?)", (now,))
        start = datetime.fromisoformat(
            db.execute("SELECT value FROM meta WHERE key='started_at'").fetchone()[0]
        )
        tasks = _paths(wd)
        registered_tasks = set(tasks)
        observed = None
        try:
            observed = (
                observations
                if observations is not None
                else None
                if os.environ.get("TH_OBSERVE") == "0"
                else proc_observer.observe()
            )
            for row in observed or []:
                if row["task"]:
                    tasks.add(Path(row["task"]))
                if row["session_id"]:
                    db.execute(
                        """INSERT INTO sessions VALUES (?,?,?,?,?,?)
                        ON CONFLICT(session_id) DO UPDATE SET claude_pid=excluded.claude_pid,
                        workspace=excluded.workspace, last_seen=excluded.last_seen""",
                        (
                            row["session_id"],
                            row["claude_pid"],
                            row["workspace"],
                            None,
                            now,
                            now,
                        ),
                    )
                db.execute(
                    """INSERT INTO processes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(pid,start) DO UPDATE SET last_seen=excluded.last_seen,
                    ended_at=NULL, kind=excluded.kind, origin=excluded.origin,
                    session_id=excluded.session_id, workspace=excluded.workspace,
                    worker_task=excluded.worker_task, task=excluded.task, cmd=excluded.cmd""",
                    (
                        row["pid"],
                        row["start"],
                        now,
                        now,
                        None,
                        row["kind"],
                        row["origin"],
                        row["session_id"],
                        row["workspace"],
                        row["worker_task"],
                        row["task"],
                        row["cmd"],
                    ),
                )
            if observed is not None:
                alive = {(r["pid"], r["start"]) for r in observed}
                for pid, identity in db.execute(
                    "SELECT pid,start FROM processes WHERE ended_at IS NULL"
                ).fetchall():
                    if (pid, identity) not in alive:
                        db.execute(
                            "UPDATE processes SET ended_at=? WHERE pid=? AND start=?",
                            (now, pid, identity),
                        )
        except Exception as exc:
            _warn(wd / "th.db", exc)
        # Reuse the existing usage calculation, including retry and duration rules.
        entries = usage_ledger.build_attempts(
            wd, Path.cwd(), task_paths=tasks, include_journals=False
        )
        for entry in entries:
            task = Path(entry.task)
            if (
                task in tasks
                and entry.started
                and (stamp := usage_ledger.parse_time(entry.started))
                and stamp >= start
            ):
                db.execute(
                    "INSERT OR REPLACE INTO attempts VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry.task,
                        entry.attempt,
                        entry.worker,
                        entry.model,
                        entry.effort,
                        entry.started,
                        entry.wall_min,
                        entry.tokens,
                        entry.result,
                        entry.override,
                    ),
                )
        for task in tasks:
            files = [
                task / n
                for n in (
                    "task.md",
                    "STATUS",
                    "DONE",
                    "FAILED",
                    "VERIFIED",
                    "VERIFY_FAILED",
                    "VERIFY_NONE",
                )
            ]
            existing = db.execute(
                "SELECT first_seen FROM tasks WHERE task=?", (str(task),)
            ).fetchone()
            if (
                not existing
                and not any(_modified(p, start) for p in files)
                and not any(r["task"] == str(task) for r in (observed or []))
            ):
                continue
            first = existing[0] if existing else now
            task_text = _read(task / "task.md")

            def field(name: str, text: str = task_text) -> str | None:
                match = re.search(rf"(?m)^{name}:\s*(.*?)\s*$", text)
                return match.group(1) if match else None

            status = _read(task / "STATUS").splitlines()
            owner = _read(task / ".th_owner").strip() or None
            state = usage_ledger.task_result(task).replace("검증 없음", "verify_none")
            db.execute(
                "INSERT OR REPLACE INTO tasks VALUES (?,?,?,?,?,?,?,?)",
                (
                    str(task),
                    owner,
                    field("task_key"),
                    field("kind"),
                    first,
                    state,
                    status[-1][:400] if status else None,
                    now,
                ),
            )
            for name in ("FAILED", "VERIFY_FAILED"):
                path = task / name
                if _modified(path, start):
                    detail = _read(path).strip()[:300]
                    if detail:
                        category = (
                            "검증 실패"
                            if name == "VERIFY_FAILED"
                            else loss_report.classify(detail)
                        )
                        if category == "기타" and re.search(
                            r"concurren|동시.*상한", detail, re.I
                        ):
                            category = "동시 실행 상한"
                        attempt = (
                            db.execute(
                                "SELECT max(attempt) FROM attempts WHERE task=?",
                                (str(task),),
                            ).fetchone()[0]
                            or 1
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO failures VALUES (?,?,?,?,?,?)",
                            (
                                str(task),
                                attempt,
                                datetime.fromtimestamp(
                                    path.stat().st_mtime, UTC
                                ).isoformat(),
                                category,
                                detail,
                                str(path),
                            ),
                        )
            folder = task if (task / "crosscheck.json").is_file() else task.parent
            manifest = folder / "crosscheck.json"
            try:
                data = json.loads(_read(manifest) or "{}")
                for name in data.get("names", []):
                    verdict = folder / name / "VERDICT.md"
                    if not _modified(verdict, start):
                        continue
                    lines = _read(verdict).splitlines()
                    last = next((x for x in reversed(lines) if x.strip()), "")
                    match = re.fullmatch(
                        r"VERDICT: (PASS|FAIL|INSUFFICIENT_EVIDENCE)", last
                    )
                    if match:
                        counter = next(
                            (
                                x.removeprefix("반례:").strip()
                                for x in lines
                                if x.startswith("반례:")
                            ),
                            "",
                        )
                        db.execute(
                            "INSERT OR IGNORE INTO adoptions VALUES (?,?,?,?,?,?)",
                            (
                                str(folder),
                                name,
                                data.get("claim"),
                                match.group(1),
                                counter,
                                datetime.fromtimestamp(
                                    verdict.stat().st_mtime, UTC
                                ).isoformat(),
                            ),
                        )
            except (ValueError, TypeError, OSError) as exc:
                _warn(manifest, exc)
        if maintenance and observed is not None and os.environ.get("TH_OBSERVE") != "0":
            try:
                from team_harness.observation_events import process_events

                process_events(db, registered_tasks, now)
            except Exception as exc:
                _warn(wd / "th.db", exc)
        workspaces = set(loss_report.owners(wd))
        workspaces.update(
            Path(r["workspace"])
            for r in (observed or [])
            if r["workspace"] and Path(r["workspace"]).is_absolute()
        )
        known = {p.name: str(p) for p in tasks}
        if (
            os.environ.get("TH_ROLE") == "worker"
            or os.environ.get("TH_SKIP_COORD_LOG") == "1"
        ):
            workspaces.clear()
        for workspace in workspaces:
            path = workspace / ".claude" / "work_log.md"
            for line, stamp in _lines(db, path, start):
                parts = [part.strip() for part in line.split("|")]
                kind = (
                    parts[1]
                    if len(parts) > 1
                    and parts[1]
                    in (
                        "지시",
                        "시작",
                        "완료",
                        "실패",
                        "결정",
                        "진단",
                        "질문",
                        "교훈",
                        "실수",
                    )
                    else "기타"
                )
                matched = next(
                    (value for name, value in known.items() if name in line), None
                )
                digest = hashlib.sha256((str(path) + "\0" + line).encode()).hexdigest()
                db.execute(
                    "INSERT OR IGNORE INTO coord_log VALUES (?,?,?,?,?,?)",
                    (
                        str(workspace),
                        stamp.isoformat(),
                        kind,
                        matched,
                        line[:400],
                        digest,
                    ),
                )
        sources = [wd / "events.log"]
        if os.environ.get("TH_ROLE") != "worker":
            sources += [
                root / ".claude" / "inbox.md" for root in loss_report.owners(wd)
            ]
        for path in sources:
            for line, stamp in _lines(db, path, start):
                digest = hashlib.sha256((str(path) + "\0" + line).encode()).hexdigest()
                parts = line.split(" | ")
                kind = parts[2] if len(parts) >= 4 else "watch"
                task = parts[1] if len(parts) >= 4 else None
                db.execute(
                    "INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                    (str(path), digest, stamp.isoformat(), task, kind, line[:400]),
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["update", "sql"])
    parser.add_argument("query", nargs="?")
    args = parser.parse_args()
    if args.command == "update":
        update()
        return 0
    query = args.query or ""
    if not re.match(r"^\s*(SELECT|WITH)\b", query, re.I):
        print("SELECT or WITH required", file=sys.stderr)
        return 2
    try:
        with sqlite3.connect(
            f"file:{(watch_dir() / 'th.db').resolve().as_posix()}?mode=ro", uri=True
        ) as db:
            db.execute("PRAGMA query_only=ON")
            cursor = db.execute(query)
            if cursor.description is None:
                raise ValueError("query must return rows")
            print("\t".join(column[0] for column in cursor.description))
            for row in cursor.fetchmany(200):
                print("\t".join("" if value is None else str(value) for value in row))
    except (sqlite3.Error, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
