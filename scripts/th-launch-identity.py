#!/usr/bin/env python3
"""Persist and check the identity of a task's launched worker."""
import json
import os
from pathlib import Path
import sys


def identity(pid: int) -> tuple[str, float] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
        if stat[0] == "Z":
            return None
        ticks = int(stat[19])
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        import time

        started = time.time() - uptime + ticks / os.sysconf("SC_CLK_TCK")
        return f"{boot}:{ticks}", started
    except (OSError, ValueError, IndexError):
        return None


def main() -> int:
    action, directory = sys.argv[1:]
    task = Path(directory)
    pid_file = task / "worker.pid"
    token_file = task / "worker.pid.start"
    try:
        pid = int(pid_file.read_text().strip())
        current = identity(pid)
        if action == "record":
            token_file.write_text(json.dumps({"pid": pid, "start": current[0] if current else None}))
            return 0
        if current is None:
            return 1
        try:
            saved = json.loads(token_file.read_text())
        except FileNotFoundError:
            # Legacy launches have no token; a newer process cannot be theirs.
            return int(current[1] > pid_file.stat().st_mtime + 1)
        return int(saved.get("pid") != pid or saved.get("start") != current[0])
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 1


if __name__ == "__main__":
    sys.exit(main())
