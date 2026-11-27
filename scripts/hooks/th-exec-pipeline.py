#!/usr/bin/env python3
"""Claude Bash adapter for the shared execution pipeline."""

from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import shlex
import sys

sys.path.insert(
    0,
    os.environ.get("TH_HARNESS_SRC", str(Path(__file__).resolve().parents[2] / "src")),
)
from team_harness.exec_map import classify
from team_harness.exec_map import split_background
from team_harness.exec_pipeline import ExecEvent
from team_harness.exec_pipeline import run_post
from team_harness.exec_pipeline import run_pre
from team_harness.exec_wrap import execute as execute_shared
from team_harness.exec_wrap import initialize_task
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


def main(payload, codex=False, agy=False):
    if agy:
        call = payload.get("toolCall", {})
        payload = {
            "hook_event_name": payload.get("hookEventName"),
            "tool_name": "Bash" if call.get("name") == "run_command" else "",
            "tool_input": {"command": call.get("args", {}).get("CommandLine", "")},
            "tool_use_id": call.get("id"),
            "session_id": payload.get("conversationId", ""),
            "cwd": payload.get("cwd", os.getcwd()),
            "tool_response": payload.get("error", ""),
        }
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
            tool="agy" if agy else shlex.split(command)[0],
            role=os.environ.get("TH_ROLE", ""),
            session=payload.get("session_id", ""),
            cwd=payload.get("cwd", os.getcwd()),
            argv=shlex.split(command),
            route="agy_hook" if agy else "claude_hook",
            context={"kind": kind},
        )
        if os.environ.get("TH_EXEC_WRAP") == "0":
            event.route = "agy_hook_disabled" if agy else "claude_hook_disabled"
            run_pre(event)
            return
        state = state_path(payload)
        event.task = str(watch_dir() / "claude-jobs" / event.event_id)
        results = run_pre(event)
        blocked = next((r for r in results if r.block), None)
        if blocked:
            if agy:
                print(json.dumps({"decision": "deny", "reason": blocked.message}))
                return
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
        # Only the explicitly observed automatic-approval mode can safely use
        # Codex's allow decision; unknown and approval-requiring modes retain
        # their original command and approval behavior.
        if codex and payload.get("permission_mode") != "bypassPermissions":
            return
        task = Path(event.task)
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
        body, background = split_background(command)
        prefix = "env " if agy else ""
        new_command = f"{prefix}TH_EXEC_ID={event.event_id} " + shlex.join(
            [
                sys.executable,
                str(runner),
                "--execute",
                str(task),
                body,
                event.owner,
                event.session,
            ]
        )
        if background:
            new_command += " &"
        if agy:
            print(
                json.dumps(
                    {"decision": "allow", "overwrite": {"CommandLine": new_command}},
                    ensure_ascii=False,
                )
            )
            return
        output = {
            "hookEventName": event_name,
            "updatedInput": dict(payload["tool_input"], command=new_command),
        }
        if codex:
            output["permissionDecision"] = "allow"
        warnings = event.warnings + [r.message for r in results if r.code and r.message]
        if warnings:
            output["additionalContext"] = "\n".join(warnings)
        print(json.dumps({"hookSpecificOutput": output}, ensure_ascii=False))
    elif event_name == "PostToolUse":
        state = state_path(payload)
        if not state.exists():
            if agy:
                print("{}")
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
        if agy:
            print("{}")


def execute(task, command, owner=None, session=None):
    if owner is not None and session is not None:
        try:
            initialize_task(Path(task), owner, session)
        except OSError:
            # execute_shared handles preparation failure by running directly.
            pass
    return execute_shared(
        task, ["bash", "-c", command], Path(__file__).resolve().parents[2]
    )


if __name__ == "__main__":
    if len(sys.argv) in (4, 6) and sys.argv[1] == "--execute":
        sys.exit(execute(*sys.argv[2:]))
    try:
        main(
            json.load(sys.stdin),
            codex="--codex" in sys.argv[1:],
            agy="--agy" in sys.argv[1:],
        )
    except Exception as exc:
        print(
            json.dumps(
                {
                    "systemMessage": f"th execution hook fail-open: {type(exc).__name__}: {exc}"
                }
            )
        )
