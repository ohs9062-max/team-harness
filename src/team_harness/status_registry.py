"""Append-only observational worker registry; never consumed by watchd."""

import fcntl
import json
import os
from pathlib import Path
import time
from typing import Any

from team_harness.coordinator_identity import process


def register(
    task: Any,
    pid: Any,
    worker: Any,
    model: Any,
    log: Any,
    owner: Any,
    coordinator: Any = None,
):
    task = Path(task).resolve()
    task.mkdir(parents=True, exist_ok=True)
    info = process(pid)
    record = {
        "path": str(task),
        "pid": pid,
        "start": info[1] if info else None,
        "boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "worker": worker,
        "model": model or "",
        "log": str(log),
        "owner": str(owner),
        "coordinator": coordinator,
        "time": time.time(),
    }
    append(record)
    return record


def append(record: Any):
    watch = Path(os.environ.get("TH_WATCH_DIR", "~/.team-harness/watch")).expanduser()
    watch.mkdir(parents=True, exist_ok=True)
    with (watch / "status_tasks.jsonl").open("a", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        f.flush()


def main():
    import sys

    task, pid, worker, model, log, owner = sys.argv[1:]
    try:
        coordinator = json.loads((Path(task) / ".th_coordinator.json").read_text())
    except (OSError, ValueError):
        coordinator = None
    register(task, int(pid), worker, model, log, owner, coordinator)


if __name__ == "__main__":
    main()
