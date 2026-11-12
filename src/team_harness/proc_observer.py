"""Best-effort snapshot of noninteractive processes belonging to this user."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from team_harness.agents.process_identity import capture_starttime
from team_harness.session_route import th_session_key


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


def has_ancestor_command(name: str) -> bool:
    """Check process ancestry without reading or modifying process state."""
    pid = os.getppid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        try:
            base = Path("/proc") / str(pid)
            args = (base / "cmdline").read_bytes().split(b"\0")
            if any(os.path.basename(os.fsdecode(arg)) == name for arg in args):
                return True
            raw = (base / "stat").read_text()
            pid = int(raw[raw.rfind(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return False


def _ancestors(pid: int, proc: dict[int, dict[str, Any]]) -> set[int]:
    seen: set[int] = set()
    while pid in proc and pid not in seen:
        seen.add(pid)
        pid = proc[pid]["parent"]
    return seen


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
                "session": int(fields[3]),
                "pgrp": int(fields[2]),
                "ticks": int(fields[19]),
                "args": args,
                "cwd": os.readlink(entry / "cwd"),
            }
        except (OSError, ValueError, IndexError):
            continue
    env_cache: dict[int, dict[str, str] | None] = {}
    session_cache: dict[tuple[str, str], tuple[str | None, str | None]] = {}
    session_index: dict[str, str] | None = None

    def session_details(
        pid: str | None, session: str | None
    ) -> tuple[str | None, str | None]:
        """Resolve coordinator metadata from Claude's per-process session record."""
        nonlocal session_index
        key = (pid or "", session or "")
        if key in session_cache:
            return session_cache[key]
        result = (session, None)
        if pid and pid.isdigit():
            try:
                path = Path.home() / ".claude" / "sessions" / f"{pid}.json"
                data = json.loads(path.read_text())
                file_session = data.get("sessionId")
                cwd = data.get("cwd") or data.get("workspace")
                identity = capture_starttime(int(pid)) if not session else None
                pid_matches = bool(
                    identity
                    and str(data.get("procStart")) == identity.rsplit(":", 1)[-1]
                )
                if (
                    session == file_session or (not session and pid_matches)
                ) and isinstance(file_session, str):
                    result = (
                        file_session,
                        cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None,
                    )
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        elif session:
            if session_index is None:
                session_index = {}
                try:
                    for path in (Path.home() / ".claude" / "sessions").glob("*.json"):
                        try:
                            data = json.loads(path.read_text())
                            sid = data.get("sessionId")
                            cwd = data.get("cwd") or data.get("workspace")
                            if (
                                isinstance(sid, str)
                                and isinstance(cwd, str)
                                and os.path.isabs(cwd)
                            ):
                                session_index[sid] = cwd
                        except (OSError, ValueError, TypeError, AttributeError):
                            continue
                except OSError:
                    pass
            result = (session, session_index.get(session))
        session_cache[key] = result
        return result

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
    uptime = float(Path("/proc/uptime").read_text().split()[0])
    ticks_per_second = os.sysconf("SC_CLK_TCK")
    helpers = {"sleep", "head", "tail", "cat", "flock", "timeout", "tee"}
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
                    "th-inbox-wait.sh",
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
        own_env = env(pid) or {}
        is_job = (
            not agent
            and not task
            and bool(own_env.get("CLAUDE_PID") or own_env.get("CLAUDE_CODE_SESSION_ID"))
            and uptime - info["ticks"] / ticks_per_second >= 30
        )
        if not agent and not task and not is_job:
            continue
        if name == "claude" and not agent:
            continue
        identity = capture_starttime(pid)
        if not identity:
            continue
        inherited: dict[str, str] = {}
        route_key = ""
        cursor = pid
        readable = False
        daemon = False
        seen: set[int] = set()
        while cursor > 1 and cursor not in seen and cursor in proc:
            seen.add(cursor)
            if any(
                os.path.basename(arg) == "th-watchd.sh"
                or arg == "team_harness.maintenance"
                for arg in proc[cursor]["args"]
            ):
                daemon = True
            values = env(cursor)
            if values is not None:
                readable = True
                if cursor == pid:
                    route_key = th_session_key(values)
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
                if not inherited.get("TH_WORKSPACE") and not inherited.get(
                    "ORCA_WORKTREE_ID"
                ):
                    resolved_session, resolved_workspace = session_details(
                        inherited.get("CLAUDE_PID"),
                        inherited.get("CLAUDE_CODE_SESSION_ID"),
                    )
                    if resolved_session:
                        inherited.setdefault("CLAUDE_CODE_SESSION_ID", resolved_session)
                    if resolved_workspace:
                        inherited["TH_WORKSPACE"] = resolved_workspace
            cursor = proc[cursor]["parent"]
        if is_job and daemon:
            continue
        worker = inherited.get("TH_WORKER_OUTPUT_DIR") or inherited.get("TH_JOB_TASK")
        session = inherited.get("CLAUDE_CODE_SESSION_ID")
        workspace = inherited.get("TH_WORKSPACE") or inherited.get("ORCA_WORKTREE_ID")
        if workspace and "::/" in workspace:
            workspace = workspace.split("::", 1)[1]
        if workspace and not os.path.isabs(workspace):
            workspace = None
        if not workspace or not session:
            resolved_session, resolved_workspace = session_details(
                inherited.get("CLAUDE_PID"), session
            )
            session = session or resolved_session
            workspace = workspace or resolved_workspace
        origin = (
            "watchd"
            if daemon
            else "worker"
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
            "route_key": route_key,
            "claude_pid": inherited.get("CLAUDE_PID"),
            "workspace": workspace or (info["cwd"] if is_job else None),
            "worker_task": worker,
            "kind": "작업"
            if is_job
            else "검수·유지보수"
            if daemon
            else "하위 워커"
            if agent
            else "job"
            if task and info["session"] == pid
            else "스크립트",
            "parent": info["parent"],
            "pgrp": info["pgrp"],
            "helper": name in helpers,
        }
    jobs = {pid: row for pid, row in candidates.items() if row["kind"] == "작업"}
    for pid, row in list(jobs.items()):
        cursor = row["parent"]
        seen: set[int] = set()
        while cursor in proc and cursor not in seen:
            seen.add(cursor)
            ancestor = candidates.get(cursor)
            if ancestor and (
                ancestor["kind"] == "하위 워커"
                or (ancestor.get("task") and ancestor["origin"] == "worker")
            ):
                jobs.pop(pid, None)
                break
            cursor = proc[cursor]["parent"]
    roots: dict[tuple[Any, ...], list[int]] = {}
    for pid, row in jobs.items():
        root = pid
        seen = set()
        while root in jobs and root not in seen:
            seen.add(root)
            parent = jobs[root]["parent"]
            if parent not in jobs or (
                row["pgrp"] and jobs[parent]["pgrp"] != row["pgrp"]
            ):
                break
            root = parent
        root_row = jobs[root]
        group_key = (
            root_row["pgrp"],
            root_row["parent"],
            root_row["claude_pid"],
            root_row["session_id"],
        )
        roots.setdefault(group_key, []).append(pid)
    selected: set[int] = set()
    for members in roots.values():
        useful = [pid for pid in members if not jobs[pid]["helper"]]
        if not useful:
            continue
        top = min(
            useful, key=lambda pid: (len(_ancestors(pid, proc)), proc[pid]["ticks"])
        )
        selected.add(top)
        descendants = [
            pid for pid in useful if pid != top and top in _ancestors(pid, proc)
        ]
        if descendants:
            oldest = min(descendants, key=lambda pid: proc[pid]["ticks"])
            jobs[top]["child_summary"] = jobs[oldest]["cmd"]
    return [
        row
        for pid, row in candidates.items()
        if row["kind"] != "작업" or pid in selected
    ]


def display_records(
    records: list[dict[str, Any]], registered_tasks: set[str], registered_pids: set[int]
) -> list[dict[str, Any]]:
    """Exclude registered process trees and group remaining processes by task."""
    grouped: dict[str, dict[str, Any]] = {}
    result: list[dict[str, Any]] = []
    for record in records:
        pid = record["pid"]
        task = record.get("task")
        if task and task in registered_tasks:
            continue
        cursor = pid
        seen: set[int] = set()
        registered = False
        while cursor > 1 and cursor not in seen:
            if cursor in registered_pids:
                registered = True
                break
            seen.add(cursor)
            try:
                stat = Path(f"/proc/{cursor}/stat").read_text()
                cursor = int(stat[stat.rfind(")") + 2 :].split()[1])
            except (OSError, ValueError, IndexError):
                break
        if registered:
            continue
        key = (
            f"job:{record.get('workspace')}:{record.get('session_id')}:{record['cmd']}"
            if record.get("kind") == "작업"
            else task or f"pid:{pid}"
        )
        if key in grouped:
            grouped[key]["count"] += 1
            if record["origin"] == "watchd" and grouped[key]["origin"] != "watchd":
                count = grouped[key]["count"]
                grouped[key].update(record, count=count)
        else:
            item = dict(record, count=1)
            grouped[key] = item
            result.append(item)
    return result
