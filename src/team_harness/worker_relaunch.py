"""Stop a worker group and relaunch through the standard recovery launcher."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

from team_harness.agents.process_identity import kill_group


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


def persisted_worker_starttime(task: Path, pid: int) -> str | None:
    """Read launch-time identity, including the original shell token format."""
    try:
        saved = json.loads((task / "worker.pid.start").read_text(encoding="utf-8"))
        if saved.get("pid") != pid or not isinstance(saved.get("start"), str):
            return None
        token = saved["start"]
        return token if token.startswith(("linux:", "lstart:")) else f"linux:{token}"
    except (OSError, ValueError, AttributeError):
        return None


def terminate_worker_group(pid: int, *, task: Path | None = None) -> None:
    """Refuse to signal a group without its persisted launch-time identity."""
    pgid = os.getpgid(pid)
    if pgid != pid or pgid == os.getpgrp():
        raise ValueError("worker does not lead an isolated process group")
    token = persisted_worker_starttime(task, pid) if task else None
    if token is None:
        raise ValueError("worker launch identity unavailable; termination deferred")
    result = kill_group(pgid, token)
    if result not in {"killed", "already-exited"}:
        raise ValueError(f"worker termination deferred: {result}")


def relaunch_worker(
    task: Path, args: list[str], owner: Path, *, auto_continue: bool = False
) -> None:
    """Use th-launch's existing STATUS recovery summary for the same task."""
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "th-launch.sh"
    env = dict(os.environ, TH_ROLLOVER="1", TH_NO_DAEMON="1", TH_WORKSPACE=str(owner))
    if auto_continue:
        env["TH_AUTO_CONTINUE"] = "1"
        session_file = task / ".th_session"
        if session_file.is_file():
            env["ORCA_PANE_KEY"] = session_file.read_text(encoding="utf-8").strip()
    subprocess.run([str(launcher), *args], env=env, check=True, capture_output=True)


class RelaunchPreflightError(ValueError):
    """A launcher preflight failure with a stable, message-independent kind."""

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


def preflight_relaunch(task: Path, args: list[str], owner: Path) -> None:
    """Check launcher policy while the existing worker is still running."""
    launcher = Path(__file__).resolve().parents[2] / "scripts" / "th-launch.sh"
    env = dict(
        os.environ,
        TH_ROLLOVER="1",
        TH_NO_DAEMON="1",
        TH_WORKSPACE=str(owner),
        TH_PREFLIGHT_ONLY="1",
        TH_PREFLIGHT_FAILURE_CODES="1",
    )
    result = subprocess.run(
        [str(launcher), *args], env=env, text=True, capture_output=True
    )
    if result.returncode:
        lines = (result.stdout + result.stderr).splitlines()
        codes = sorted(
            line.removeprefix("TH_POLICY_CODE=")
            for line in lines
            if line.startswith("TH_POLICY_CODE=")
        )
        message = "\n".join(
            line for line in lines if not line.startswith("TH_POLICY_CODE=")
        ).strip()
        kind = "policy:" + ",".join(codes) if codes else f"exit:{result.returncode}"
        raise RelaunchPreflightError(message, kind)
