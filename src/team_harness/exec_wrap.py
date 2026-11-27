"""Shared synchronous task preparation and execution for execution adapters."""

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from types import FrameType

from team_harness.agents.process_identity import capture_starttime
from team_harness.exec_pipeline import ExecEvent
from team_harness.internal_python import caller_environment
from team_harness.workspace_archive import watch_dir


def prepare_task(event: ExecEvent, category: str) -> Path:
    task = watch_dir() / category / event.event_id
    task.mkdir(parents=True, mode=0o700)
    event.task = str(task)
    (task / ".th_owner").write_text(event.owner + "\n")
    (task / ".th_session").write_text(event.session + "\n")
    return task


def warning(message: str) -> None:
    print(f"th execution fail-open: {message}", file=sys.stderr)


def _prepare(task: Path, root: Path) -> None:
    with (task / "STATUS").open("a") as f:
        f.write(datetime.now().strftime("%F %T") + " START step=command replay=never\n")
    watch = watch_dir()
    watch.mkdir(parents=True, exist_ok=True)
    with (watch / "tasks.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with (watch / "tasks.txt").open("a") as registry:
            registry.write(str(task) + "\n")
    if os.environ.get("TH_NO_DAEMON") != "1":
        subprocess.Popen(
            [str(root / "scripts/th-watchd.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def execute(task: str | Path, argv: list[str], root: Path | None = None) -> int:
    task = Path(task)
    root = root or Path(__file__).resolve().parents[2]
    runner = None
    try:
        _prepare(task, root)
        read_fd, write_fd = os.pipe()
        try:
            env = dict(caller_environment(), TH_JOB_START_NOTIFY_FD=str(write_fd))
            try:
                runner = subprocess.Popen(
                    [
                        str(root / "scripts/th-job-start.sh"),
                        str(task),
                        "--wait",
                        "--",
                        *argv,
                    ],
                    env=env,
                    pass_fds=(write_fd,),
                )
            finally:
                os.close(write_fd)
            previous = {}

            def forward(signum: int, _frame: FrameType | None) -> None:
                if runner is not None and runner.poll() is None:
                    runner.send_signal(signum)

            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, forward)
            with os.fdopen(read_fd, "rb", buffering=0) as notification:
                read_fd = -1
                ready = notification.read(1)
                started = notification.read(1) if ready == b"R" else b""
            rc = runner.wait()
            for sig, handler in previous.items():
                signal.signal(sig, handler)
        finally:
            if read_fd != -1:
                os.close(read_fd)
        if ready == b"R" and not started:
            runner = None
            raise RuntimeError(f"runner failed before command (rc={rc})")
        if ready != b"R" or started != b"S":
            warning(f"runner start state unknown (rc={rc}); command not retried")
    except Exception as exc:  # noqa: BLE001
        if runner is None:
            warning(
                f"preparation failed ({type(exc).__name__}: {exc}); running command directly"
            )
            rc = subprocess.call(argv, env=caller_environment())
        else:
            warning(
                f"runner start state unknown ({type(exc).__name__}: {exc}); command not retried"
            )
            rc = runner.wait()
    _record(task, rc, "logs/job.log")
    return rc


def _record(task: Path, rc: int, output: str) -> None:
    for path, content in (
        (task / "exit_code", str(rc) + "\n"),
        (
            task / "STATUS",
            datetime.now().strftime("%F %T")
            + f" END step=command rc={rc} out={output}\n",
        ),
        (task / ("DONE" if rc == 0 else "FAILED"), f"command rc={rc}\n"),
    ):
        try:
            with path.open("a" if path.name == "STATUS" else "w") as f:
                f.write(content)
        except OSError as exc:
            warning(f"cannot record {path.name}: {exc}")


def execute_direct(task: str | Path, argv: list[str], root: Path | None = None) -> int:
    """Run a PATH shim command in the caller's session with inherited streams."""
    task = Path(task)
    root = root or Path(__file__).resolve().parents[2]
    try:
        _prepare(task, root)
    except Exception as exc:  # noqa: BLE001
        warning(
            f"preparation failed ({type(exc).__name__}: {exc}); running command directly"
        )
    child = subprocess.Popen(
        argv, env=dict(caller_environment(), TH_JOB_TASK=str(task))
    )
    previous = {}

    def forward(signum: int, _frame: FrameType | None) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    try:
        for sig in (signal.SIGINT, signal.SIGQUIT):
            previous[sig] = signal.signal(sig, signal.SIG_IGN)
        for sig in (signal.SIGTERM, signal.SIGHUP):
            previous[sig] = signal.signal(sig, forward)
        try:
            (task / "job.pid").write_text(f"{child.pid}\n")
            start = capture_starttime(child.pid)
            if start:
                (task / "job.pid.start").write_text(
                    json.dumps({"pid": child.pid, "start": start})
                )
        except Exception as exc:  # noqa: BLE001
            warning(f"cannot record child PID: {exc}")
        rc = child.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    rc = 128 - rc if rc < 0 else rc
    _record(task, rc, "-")
    return rc
