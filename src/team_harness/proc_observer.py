"""Best-effort snapshot of noninteractive processes belonging to this user."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from team_harness.agents.process_identity import capture_starttime


def _task(path: str) -> str | None:
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            return None
        if not candidate.is_dir():
            candidate = candidate.parent
        for parent in (candidate, *candidate.parents):
            if any(
                (parent / name).is_file() for name in ("STATUS", "job.pid", "task.md")
            ):
                return str(parent)
    except OSError:
        pass
    return None


def observe() -> list[dict[str, Any]]:
    """Return a snapshot; individual processes can disappear while being read."""
    if os.environ.get("TH_OBSERVE") == "0":
        return []
    proc: dict[int, dict[str, Any]] = {}
    uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            pid = int(entry.name)
            raw = (entry / "stat").read_text()
            fields = raw[raw.rfind(")") + 2 :].split()
            if fields[0] == "Z":
                continue
            args = [
                x.decode(errors="replace")
                for x in (entry / "cmdline").read_bytes().split(b"\0")
                if x
            ]
            if not args:
                continue
            proc[pid] = {
                "parent": int(fields[1]),
                "args": args,
                "cwd": os.readlink(entry / "cwd"),
            }
        except (OSError, ValueError, IndexError):
            continue
    env_cache: dict[int, dict[str, str] | None] = {}

    def env(pid: int) -> dict[str, str] | None:
        if pid not in env_cache:
            try:
                env_cache[pid] = dict(
                    item.decode(errors="replace").split("=", 1)
                    for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                    if b"=" in item
                )
            except OSError:
                env_cache[pid] = None
        return env_cache[pid]

    candidates: dict[int, dict[str, Any]] = {}
    for pid, info in proc.items():
        args = info["args"]
        name = os.path.basename(args[0].split(" ", 1)[0])
        if (
            name
            in {
                "code",
                "chrome",
                "chrome_crashpad_handler",
                "chromium",
                "firefox",
                "pet",
            }
            or (
                name in {"bash", "sh", "zsh"}
                and (
                    len(args) == 1 or "--init-file" in args[0] or "--init-file" in args
                )
            )
            or "team_harness.maintenance" in args
        ):
            continue
        if pid == os.getpid() or any(
            any(
                name in a
                for name in (
                    "th-status.sh",
                    "journal-hook.sh",
                    "th-watchd.sh",
                    "th-watch.sh",
                )
            )
            for a in args
        ):
            continue
        agent = (
            (name.startswith("codex") and "exec" in args[1:])
            or name == "agy"
            or (name == "claude" and any(a in ("-p", "--print") for a in args[1:]))
            or name in ("opencode", "pi")
        )
        if name.startswith("codex") and not agent:
            continue
        task = _task(info["cwd"]) or next(
            (p for a in args[1:] if (p := _task(a))), None
        )
        if not agent and not task:
            continue
        if name == "claude" and not agent:
            continue
        identity = capture_starttime(pid)
        if not identity:
            continue
        inherited: dict[str, str] = {}
        cursor = pid
        readable = False
        seen: set[int] = set()
        while cursor > 1 and cursor not in seen and cursor in proc:
            seen.add(cursor)
            values = env(cursor)
            if values is not None:
                readable = True
                for key in (
                    "TH_ROLE",
                    "TH_WORKER_OUTPUT_DIR",
                    "TH_JOB_TASK",
                    "TH_WORKSPACE",
                    "ORCA_WORKTREE_ID",
                    "CLAUDE_CODE_SESSION_ID",
                    "CLAUDE_PID",
                ):
                    if values.get(key) and not inherited.get(key):
                        inherited[key] = values[key]
            cursor = proc[cursor]["parent"]
        worker = inherited.get("TH_WORKER_OUTPUT_DIR") or inherited.get("TH_JOB_TASK")
        session = inherited.get("CLAUDE_CODE_SESSION_ID")
        workspace = inherited.get("TH_WORKSPACE") or inherited.get("ORCA_WORKTREE_ID")
        if workspace and "::/" in workspace:
            workspace = workspace.split("::", 1)[1]
        origin = (
            "worker"
            if worker or inherited.get("TH_ROLE") == "worker"
            else "coordinator"
            if session or inherited.get("CLAUDE_PID")
            else "user"
            if readable
            else "unknown"
        )
        candidates[pid] = {
            "pid": pid,
            "start": identity,
            "cmd": " ".join(args)[:120],
            "cwd": info["cwd"],
            "task": task,
            "origin": origin,
            "session_id": session,
            "claude_pid": inherited.get("CLAUDE_PID"),
            "workspace": workspace,
            "worker_task": worker,
            "kind": "하위 워커" if agent and worker else "job" if task else "스크립트",
            "parent": info["parent"],
        }
    # One representative per same-task process tree.
    return [
        row
        for pid, row in candidates.items()
        if not (
            row["task"]
            and row["parent"] in candidates
            and candidates[row["parent"]]["task"] == row["task"]
        )
    ]
