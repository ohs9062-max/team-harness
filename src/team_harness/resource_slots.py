"""Atomic cross-process worker admission shared by every launch path."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import sys
import uuid

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.process_identity import probe_group
from team_harness.agents.process_identity import ProcessProbeError
from team_harness.launch_policy import _active_workers
from team_harness.launch_policy import _pid_alive
from team_harness.launch_policy import check
from team_harness.worker_digest import quota_lock_active
from team_harness.workspace_archive import watch_dir


class AdmissionError(RuntimeError):
    """A shared resource policy prevented spawning a worker."""


def worker_family(worker: str) -> str:
    return "agy" if worker == "antigravity" else worker


@contextmanager
def ledger(directory: Path):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "admission.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = directory / "admission.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, dict):
                raise ValueError("invalid admission ledger")
        except FileNotFoundError:
            rows = {}
        # A corrupt ledger is an error, never an empty set of running workers.
        yield rows
        temporary = directory / ".admission.json.tmp"
        temporary.write_text(json.dumps(rows) + "\n", encoding="utf-8")
        temporary.replace(path)


def active(row: dict) -> bool:
    token = row.get("start")
    if row.get("bound"):
        try:
            # Some CLIs reparent without leading the original group. Their
            # own launch identity still proves that an active worker exists.
            if (
                token
                and capture_starttime(int(row["pid"])) == token
                and _pid_alive(int(row["pid"]))
            ):
                return True
            pgid = int(row.get("pgid", row["pid"]))
            group_token = row.get("group_start", token)
            state = probe_group(pgid, group_token)
            return state.alive and state.verdict != "identity_mismatch"
        except ProcessProbeError:
            return True
    if not token:
        try:
            os.kill(int(row["pid"]), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    return capture_starttime(int(row["pid"])) == token


def reserve(
    task: Path,
    worker: str,
    model: str = "",
    effort: str = "",
    *,
    owner_pid: int | None = None,
    directory: Path | None = None,
) -> str:
    directory = directory or watch_dir()
    worker = worker_family(worker)
    with ledger(directory) as rows:
        for key in list(rows):
            if not active(rows[key]):
                del rows[key]
        family = [row for row in rows.values() if row["worker"] == worker]
        tasks = [row["task"] for row in family]
        represented = set(tasks)
        tasks.extend(
            path
            for path in _active_workers(task, worker, directory, full_paths=True)
            if path not in represented
        )
        violations = check(
            task,
            worker,
            model,
            effort,
            directory,
            active_workers=[Path(p).name for p in sorted(tasks)],
        )
        violations = [v for v in violations if v.code in {"parallel", "budget"}]
        if os.environ.get("TH_IGNORE_QUOTA_LOCK") != "1" and quota_lock_active(
            directory / f"quota_lock.{worker}"
        ):
            raise AdmissionError(f"{worker}: quota lock active")
        if violations:
            raise AdmissionError("; ".join(v.message for v in violations))
        pid = owner_pid or os.getpid()
        key = uuid.uuid4().hex
        rows[key] = {
            "pid": pid,
            "start": capture_starttime(pid),
            "worker": worker,
            "task": str(task.resolve()),
            "bound": False,
        }
    return key


def bind(key: str, pid: int, *, directory: Path | None = None) -> None:
    with ledger(directory or watch_dir()) as rows:
        row = rows[key]
        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            pgid = pid
        # Never retain the coordinator's own group as a worker reservation.
        if pgid == os.getpgrp():
            pgid = pid
        row.update(
            pid=pid,
            start=capture_starttime(pid),
            bound=True,
            pgid=pgid,
            group_start=capture_starttime(pgid),
        )


def release(key: str, *, directory: Path | None = None) -> None:
    with ledger(directory or watch_dir()) as rows:
        rows.pop(key, None)


def main() -> int:
    try:
        action, *args = sys.argv[1:]
        if action == "reserve":
            task, worker, model, effort, pid = args
            print(reserve(Path(task), worker, model, effort, owner_pid=int(pid)))
        elif action == "bind":
            bind(args[0], int(args[1]))
        elif action == "release":
            release(args[0])
        else:
            raise ValueError("unknown admission action")
        return 0
    except (OSError, ValueError, KeyError, AdmissionError) as exc:
        print(f"worker admission failed: {exc}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
