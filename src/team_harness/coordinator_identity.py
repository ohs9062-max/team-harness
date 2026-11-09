#!/usr/bin/env python3
"""Conservative launcher ancestry identity; never infer ownership from a waiter."""

import os
from pathlib import Path
from typing import Any

from team_harness.coordinator_models import resolve_model
from team_harness.coordinator_models import session_for


def process(pid: Any):
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rfind(")") + 2 :].split()
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (
            int(fields[1]),
            fields[19],
            fields[0],
            [a.decode(errors="replace") for a in args if a],
        )
    except (OSError, ValueError, IndexError):
        return None


def capture(pid: Any, session: Any):
    while pid > 1:
        info = process(pid)
        if not info:
            break
        parent, start, state, args = info
        name = os.path.basename(args[0]) if args else ""
        if name in ("codex", "claude", "agy") or name.startswith("codex-"):
            model = ""
            for i, arg in enumerate(args):
                if arg in ("-m", "--model") and i + 1 < len(args):
                    model = args[i + 1]
                elif arg.startswith("--model="):
                    model = arg.split("=", 1)[1]
            try:
                boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            except OSError:
                return None
            return {
                "pid": pid,
                "start": start,
                "boot": boot,
                "session": session,
                "model": model,
            }
        pid = parent
    return None


def alive(meta: Any):
    try:
        info = process(int(meta["pid"]))
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return bool(
            info
            and info[1] == str(meta["start"])
            and info[2] != "Z"
            and meta["boot"] == boot
        )
    except (OSError, KeyError, TypeError, ValueError):
        return False


def capture_current(session: Any, model: Any):
    """The harness owns this process and knows the configured coordinator model."""
    pid = os.getpid()
    info = process(pid)
    if not info:
        return None
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None
    return {
        "pid": pid,
        "start": info[1],
        "boot": boot,
        "session": session,
        "model": model,
    }


def live_coordinators():
    """Discover interactive pane CLIs, excluding workers and their descendants."""
    try:
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        import time

        boot_epoch = time.time() - uptime
        ticks = os.sysconf("SC_CLK_TCK")
    except (OSError, ValueError):
        return []
    processes = {}
    for entry in Path("/proc").glob("[0-9]*"):
        pid = int(entry.name)
        info = process(pid)
        if info:
            processes[pid] = info
    candidates = []
    for pid, (parent, start, state, args) in processes.items():
        name = os.path.basename(args[0]) if args else ""
        if state == "Z" or not (
            name in ("codex", "claude", "agy") or name.startswith("codex-")
        ):
            continue
        try:
            env = dict(
                item.split(b"=", 1)
                for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
                if b"=" in item
            )
            pane = env.get(b"ORCA_PANE_KEY", b"").decode()
            session = (
                pane
                or env.get(b"CLAUDE_CODE_SESSION_ID", b"").decode()
                or env.get(b"TH_SESSION_KEY", b"").decode()
            )
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except (OSError, UnicodeError):
            continue
        if not session or env.get(b"TH_ROLE", b"").lower() == b"worker":
            continue
        # A nested agent CLI belongs to its enclosing CLI, not a new coordinator.
        ancestor = parent
        nested = False
        while ancestor > 1 and ancestor in processes:
            pparent, _, _, pargs = processes[ancestor]
            pname = os.path.basename(pargs[0]) if pargs else ""
            if pname in ("codex", "claude", "agy") or pname.startswith("codex-"):
                nested = True
                break
            ancestor = pparent
        if nested or any(a in ("exec", "-p", "--print") for a in args[1:]):
            continue
        model = ""
        for i, arg in enumerate(args):
            if arg in ("-m", "--model") and i + 1 < len(args):
                model = args[i + 1]
            elif arg.startswith("--model="):
                model = arg.split("=", 1)[1]
        vendor_session = session_for(name, args, env, pid, start)
        model = resolve_model(name, vendor_session, pid, start) or model
        meta = {
            "pid": pid,
            "start": start,
            "boot": boot,
            "session": session,
            "pane": pane,
            "model": model,
            "cwd": cwd,
            "started_at": boot_epoch + int(start) / ticks,
        }
        if alive(meta):
            candidates.append(meta)
    # One pane is one displayed session. Concurrent independent CLIs in that
    # pane cannot safely own a legacy task without additional evidence.
    by_session = {}
    for meta in sorted(candidates, key=lambda c: int(c["start"])):
        previous = by_session.setdefault(meta["session"], meta)
        if previous is not meta:
            previous["ambiguous"] = True
    return list(by_session.values())


def git_common_dir(directory: Any):
    """Find a repository's common git directory without invoking git."""
    try:
        if not Path(directory).is_absolute() or not Path(directory).is_dir():
            return None
        path = Path(directory).resolve()
        for parent in (path, *path.parents):
            dotgit = parent / ".git"
            if dotgit.is_dir():
                gitdir = dotgit.resolve()
            elif dotgit.is_file():
                text = dotgit.read_text().strip()
                if not text.startswith("gitdir: "):
                    return None
                gitdir = (parent / text[8:]).resolve()
                if not gitdir.is_dir():
                    return None
            else:
                continue
            common = gitdir / "commondir"
            result = (
                (gitdir / common.read_text().strip()).resolve()
                if common.is_file()
                else gitdir
            )
            return result if result.is_dir() else None
    except (OSError, ValueError, RuntimeError):
        pass
    return None


def session_owner(task: Any, candidates: Any):
    """Restore legacy ownership only for an unambiguous pre-existing pane CLI."""
    try:
        session_file = Path(task) / ".th_session"
        session = session_file.read_text().strip()
        stamp = session_file.stat().st_mtime
        matches = [
            c
            for c in candidates
            if session and c.get("pane") == session and c["started_at"] <= stamp
        ]
        return (
            matches[0]
            if len(matches) == 1 and not matches[0].get("ambiguous")
            else None
        )
    except OSError:
        return None
