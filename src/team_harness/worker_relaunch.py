"""Stop a worker group and relaunch through the standard recovery launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import time


def launch_arguments(task: Path, watch_dir: Path) -> list[str]:
    """Recover the last launch settings before disturbing the running worker."""
    records = (watch_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines()
    for line in reversed(records):
        try:
            row = json.loads(line)
            if Path(row["task"]).resolve() != task.resolve():
                continue
            values = [row[key] for key in ("worker", "model", "effort")]
            if all(isinstance(value, str) and value for value in values):
                return [str(task), *values, (task / ".th_worktree").read_text().strip()]
        except (KeyError, TypeError, ValueError):
            continue
    raise ValueError("no launch settings for task")


def terminate_worker_group(pid: int) -> None:
    """Terminate only the worker's isolated process group, with a ten-second grace."""
    pgid = os.getpgid(pid)
    if pgid != pid or pgid == os.getpgrp():
        raise ValueError("worker does not lead an isolated process group")
    os.killpg(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def relaunch_worker(task: Path, args: list[str], owner: Path) -> None:
    """Use th-launch's existing STATUS recovery summary for the same task."""
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "th-launch.sh"
    env = dict(os.environ, TH_ROLLOVER="1", TH_NO_DAEMON="1", TH_WORKSPACE=str(owner))
    subprocess.run([str(launcher), *args], env=env, check=True, capture_output=True)
