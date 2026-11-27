"""Fail-open execution event adapter for shell entry points."""

from __future__ import annotations

from datetime import datetime
from datetime import UTC
import fcntl
import json
import os
from pathlib import Path
import sys

from team_harness.exec_pipeline import ExecEvent
from team_harness.exec_pipeline import run_post
from team_harness.exec_pipeline import run_pre


def state_path(task: str, route: str) -> Path:
    name = ".th_job_exec_event" if route == "th_job_start" else ".th_exec_event"
    return Path(task) / name


def pre(task: str, route: str, tool: str, argv: list[str]) -> None:
    if os.environ.get("TH_EXEC_ID") or os.environ.get("TH_EXEC_EVENT_ID"):
        return
    event = ExecEvent(
        task=task,
        route=route,
        tool=tool,
        argv=argv,
        cwd=os.getcwd(),
        role=os.environ.get("TH_ROLE", ""),
        session=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        output_paths=[os.environ["TH_PIPELINE_OUTPUT"]]
        if route == "th_job_start" and os.environ.get("TH_PIPELINE_OUTPUT")
        else [],
    )
    run_pre(event)
    event.task = task
    path = state_path(task, route)
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(event.to_json())


def post(task: str, route: str, result: str) -> None:
    path = state_path(task, route)
    if not path.exists():
        return
    fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
    with os.fdopen(fd, "r+", encoding="utf-8") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        event = ExecEvent(**json.load(stream))
        if event.ended_at:
            return
        try:
            event.exit_code = int(result)
        except ValueError:
            try:
                event.exit_code = int((Path(task) / "exit_code").read_text().strip())
            except (ValueError, OSError):
                event.exit_code = None
        event.ended_at = datetime.now(UTC).isoformat()
        event.output_paths = event.output_paths or [
            str(
                Path(task)
                / (f"{event.tool}.log" if route == "th_launch" else "logs/job.log")
            )
        ]
        event.result_paths = [
            str(Path(task) / name)
            for name in ("DONE", "FAILED", "exit_code")
            if (Path(task) / name).exists()
        ]
        # Persist the claim while locked so repeated notifications do not post twice.
        stream.seek(0)
        stream.write(event.to_json())
        stream.truncate()
        stream.flush()
        run_post(event)


def main() -> int:
    try:
        if len(sys.argv) >= 6 and sys.argv[1] == "pre":
            pre(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5:])
        elif len(sys.argv) == 5 and sys.argv[1] == "post":
            post(sys.argv[2], sys.argv[3], sys.argv[4])
        else:
            return 2
    except Exception:  # noqa: BLE001
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
