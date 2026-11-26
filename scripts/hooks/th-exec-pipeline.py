#!/usr/bin/env python3
"""Claude Bash adapter for the shared execution pipeline."""

from datetime import datetime
from datetime import UTC
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(
    0,
    os.environ.get("TH_HARNESS_SRC", str(Path(__file__).resolve().parents[2] / "src")),
)
from team_harness.exec_map import classify
from team_harness.exec_pipeline import ExecEvent
from team_harness.exec_pipeline import run_post
from team_harness.exec_pipeline import run_pre
from team_harness.workspace_archive import watch_dir


def ancestor_is_th():
    pid = os.getppid()
    for _ in range(64):
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            names = [Path(x.decode(errors="replace")).name for x in args[:2]]
            if any(name in ("th-launch.sh", "th-job-start.sh") for name in names):
                return True
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            pid = int(stat[1])
            if pid <= 1:
                break
        except (OSError, ValueError, IndexError):
            break
    return False


def state_path(payload):
    key = str(payload.get("session_id", "")) + ":" + str(payload.get("tool_use_id", ""))
    if not payload.get("tool_use_id"):
        raise ValueError("tool_use_id is required")
    return (
        watch_dir()
        / "claude-exec"
        / (hashlib.sha256(key.encode()).hexdigest() + ".json")
    )


def main(payload):
    event_name = payload.get("hook_event_name")
    if (
        payload.get("tool_name") != "Bash"
        or os.environ.get("TH_ROLE") == "worker"
        or ancestor_is_th()
    ):
        return
    if event_name == "PreToolUse":
        command = payload.get("tool_input", {}).get("command", "")
        kind = classify(command)
        if not kind:
            return
        event = ExecEvent(
            tool=shlex.split(command)[0],
            role=os.environ.get("TH_ROLE", ""),
            session=payload.get("session_id", ""),
            cwd=payload.get("cwd", os.getcwd()),
            argv=shlex.split(command),
            route="claude_hook",
            context={"kind": kind},
        )
        if os.environ.get("TH_EXEC_WRAP") == "0":
            event.route = "claude_hook_disabled"
            run_pre(event)
            return
        state = state_path(payload)
        event.task = str(watch_dir() / "claude-jobs" / event.event_id)
        results = run_pre(event)
        blocked = next((r for r in results if r.block), None)
        if blocked:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": event_name,
                            "permissionDecision": "deny",
                            "permissionDecisionReason": blocked.message,
                        }
                    }
                )
            )
            return
        task = watch_dir() / "claude-jobs" / event.event_id
        task.mkdir(parents=True, mode=0o700)
        event.task = str(task)
        (task / ".th_owner").write_text(event.owner + "\n")
        (task / ".th_session").write_text(event.session + "\n")
        state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(state, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(event.to_json())
        runner = Path(__file__).resolve()
        # Installed hook uses the source runner so paths remain stable after deployment.
        source = Path(
            os.environ.get("TH_HARNESS_SRC", str(runner.parents[2] / "src"))
        ).parent
        runner = source / "scripts/hooks/th-exec-pipeline.py"
        new_command = shlex.join(
            [sys.executable, str(runner), "--execute", str(task), command]
        )
        output = {
            "hookEventName": event_name,
            "updatedInput": dict(payload["tool_input"], command=new_command),
        }
        warnings = event.warnings + [r.message for r in results if r.code and r.message]
        if warnings:
            output["additionalContext"] = "\n".join(warnings)
        print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))
    elif event_name == "PostToolUse":
        state = state_path(payload)
        if not state.exists():
            return
        event = ExecEvent(**json.loads(state.read_text()))
        task = Path(event.task)
        try:
            event.exit_code = int((task / "exit_code").read_text().strip())
        except (OSError, ValueError):
            event.exit_code = None
        event.ended_at = datetime.now(UTC).isoformat()
        event.message = str(payload.get("tool_response", ""))[:4096]
        event.output_paths = [str(task / "logs/job.log")]
        run_post(event)
        state.unlink()


def execute(task, command):
    task = Path(task)
    root = Path(__file__).resolve().parents[2]
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
    rc = subprocess.call(
        [
            str(root / "scripts/th-job-start.sh"),
            str(task),
            "--wait",
            "--",
            "bash",
            "-c",
            command,
        ]
    )
    (task / "exit_code").write_text(str(rc) + "\n")
    with (task / "STATUS").open("a") as f:
        f.write(
            datetime.now().strftime("%F %T")
            + f" END step=command rc={rc} out=logs/job.log\n"
        )
    (task / ("DONE" if rc == 0 else "FAILED")).write_text(f"command rc={rc}\n")
    return rc


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--execute":
        sys.exit(execute(sys.argv[2], sys.argv[3]))
    try:
        main(json.load(sys.stdin))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "systemMessage": f"th execution hook fail-open: {type(exc).__name__}: {exc}"
                }
            )
        )
