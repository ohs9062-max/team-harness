"""Stop only processes whose identity can be attributed to the task."""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import select
import signal
import sys

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.process_identity import kill_group
from team_harness.step_records import parse_task
from team_harness.worker_relaunch import terminate_worker_group
from team_harness.workspace_archive import watch_dir


def stop_observed(task: Path) -> list[str]:
    """Stop identity-verified PATH-shim processes and report unconfirmed exits."""
    ledger = watch_dir() / "exec_pipeline.jsonl"
    try:
        lines = ledger.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    failures: list[str] = []
    seen: set[tuple[int, str]] = set()
    for line in lines:
        try:
            row = json.loads(line)
            event = row["event"]
            pid, start = event["pid"], event["process_start"]
            if (
                row.get("phase") != "pre"
                or event.get("route") != ""
                or event.get("tool") not in {"codex", "agy", "claude"}
                or Path(event["task"]).resolve() != task.resolve()
                or not isinstance(pid, int)
                or pid <= 1
                or pid == os.getpid()
                or not isinstance(start, str)
                or not start
                or (pid, start) in seen
            ):
                continue
            seen.add((pid, start))
        except (ValueError, KeyError, TypeError, AttributeError):
            continue
        try:
            fd = os.pidfd_open(pid)
            try:
                if capture_starttime(pid) != start:
                    continue
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                poller = select.poll()
                poller.register(fd, select.POLLIN)
                events = poller.poll(10_000)
                if not events:
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                    events = poller.poll(10_000)
                if not events or not events[0][1] & (select.POLLIN | select.POLLHUP):
                    failures.append(f"observed PID {pid}: termination unconfirmed")
            finally:
                os.close(fd)
        except ProcessLookupError:
            continue
        except OSError as exc:
            failures.append(f"observed PID {pid}: {exc}")
    return failures


def stop(task: Path, reason: str) -> list[str]:
    marker = task / "STOPPED_BY_USER"
    reason = reason.replace("\n", " ")
    marker.write_text(f"incomplete 처리 중 | {reason}\n", encoding="utf-8")
    failures: list[str] = []
    # watchd checks this notification marker before it inspects task state.
    (task / ".th_daemon.verify_notice").write_text(
        "사용자 정지로 알림 보류\n", encoding="utf-8"
    )
    for adopt in (task / ".th_adopt", task / ".th_adopt_retry"):
        if not adopt.is_dir():
            continue
        for child in adopt.iterdir():
            if not child.is_dir():
                continue
            try:
                with (child / ".th_launch.lock").open("a") as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    failures.extend(f"{child}: {item}" for item in stop(child, reason))
            except (OSError, RuntimeError, ValueError) as exc:
                failures.append(f"{child}: 판정 불가: {exc}")
    (task / ".th_daemon.settled").unlink(missing_ok=True)
    for name in ("worker.pid", "job.pid"):
        pid_file = task / name
        if not pid_file.exists():
            continue
        pid = 0
        try:
            pid = int(pid_file.read_text().strip())
            if pid <= 1:
                raise ValueError("invalid pid")
            if name == "worker.pid":
                terminate_worker_group(pid, task=task)
            else:
                env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if (
                    f"TH_JOB_TASK={task}".encode() not in env
                    or os.getpgid(pid) != pid
                    or pid == os.getpgrp()
                ):
                    raise ValueError("job identity or process group mismatch")
                saved = json.loads((task / "job.pid.start").read_text())
                if saved.get("pid") != pid or not isinstance(saved.get("start"), str):
                    raise ValueError("job launch identity unavailable")
                result = kill_group(pid, saved["start"])
                if result not in {"killed", "already-exited"}:
                    raise ValueError(f"job termination deferred: {result}")
        except ProcessLookupError:
            continue
        except (OSError, ValueError, RuntimeError) as exc:
            failures.append(f"{name} PID {pid or '?'}: {exc}")
    failures.extend(stop_observed(task))
    if failures:
        detail = "; ".join(failures).replace("\n", " ")
        marker.write_text(f"incomplete {detail} | {reason}\n", encoding="utf-8")
        with (task / "STATUS").open("a", encoding="utf-8") as status:
            status.write(f"{datetime.now():%F %T} STOP_INCOMPLETE {detail}\n")
        return failures
    open_steps = [
        record.name for record in parse_task(task).values() if record.state == "open"
    ]
    with (task / "STATUS").open("a", encoding="utf-8") as status:
        for name in open_steps or ["user_stop"]:
            status.write(
                f"{datetime.now():%F %T} END step={name} rc=130 out=STOPPED_BY_USER\n"
            )
    marker.write_text(f"{datetime.now():%F %T} | {reason}\n", encoding="utf-8")
    return []


if __name__ == "__main__":
    failures = stop(Path(sys.argv[1]), sys.argv[2])
    if failures:
        print("STOP_INCOMPLETE: " + "; ".join(failures), file=sys.stderr)
        sys.exit(2)
