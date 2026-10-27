#!/usr/bin/env python3
"""Read-only process discovery shared by status and watch."""
import os
import re
import sys
from pathlib import Path


def live_processes(task):
    task = Path(task).resolve()
    direct, tagged, guessed = set(), set(), set()
    for name in ("worker.pid", "job.pid"):
        try:
            values = (task / name).read_text().split()
        except OSError:
            continue
        for value in values:
            try:
                if value.isdecimal() and int(value) > 0:
                    pid = int(value)
                    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                    if stat[0] != "Z":
                        direct.add(pid)
            except (OSError, ValueError, IndexError):
                continue
    tag = f"TH_JOB_TASK={task}".encode()
    excluded = ("th-status", "th-watch", "th-inbox-wait", "tail", "less", "grep", "vim", "nvim", "emacs", "nano", "code", "claude", "codex", "shell-snapshots")
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            pid = int(proc.name)
            stat = (proc / "stat").read_text().rsplit(") ", 1)[1].split()
            if stat[0] == "Z":
                continue
            if tag in (proc / "environ").read_bytes().split(b"\0"):
                tagged.add(pid)
            if int(stat[3]) != pid:
                continue
            args = (proc / "cmdline").read_bytes().split(b"\0")
            cmd = b" ".join(args).decode(errors="replace")
            is_shell = os.path.basename(os.fsdecode(args[0])) in ("sh", "bash", "dash", "zsh")
            matches = task.name in cmd
            if is_shell:
                matches = bool(re.search(r"/" + re.escape(task.name) + r"(?:/|\b)", cmd))
            if matches and not any(x in cmd.lower() for x in excluded):
                guessed.add(pid)
        except (OSError, ValueError, IndexError):
            continue
    if direct or tagged:
        return sorted(direct | tagged), False
    return sorted(guessed), bool(guessed)


def current_markers(task, pids):
    """Return marker names belonging to the current live attempt."""
    task = Path(task)
    markers = [task / "DONE", task / "FAILED", task / "VERIFY_FAILED"]
    markers.extend(task.glob("FAILED.*"))
    markers = [path for path in markers if path.is_file()]
    if not pids:
        return {path.name for path in markers}
    starts = []
    try:
        btime = next(int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines() if line.startswith("btime "))
        ticks = os.sysconf("SC_CLK_TCK")
        for pid in pids:
            try:
                fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
                starts.append(btime + int(fields[19]) / ticks)
            except (OSError, ValueError, IndexError):
                continue
    except (OSError, StopIteration, ValueError):
        pass
    if starts:
        started = min(starts)
    else:
        try:
            started = (task / "job.pid").stat().st_mtime
        except OSError:
            return {path.name for path in markers}
    return {path.name for path in markers if path.stat().st_mtime >= started}


def effective_terminal(task, terminal, pids):
    if not pids or not terminal:
        return terminal
    task = Path(task)
    names = current_markers(task, pids)
    failures = [p for p in [task / "FAILED", *task.glob("FAILED.*")] if p.is_file()]
    if terminal == "FAILED" and failures and not any(p.name in names for p in failures):
        return "DONE" if "DONE" in names else ""
    if terminal == "DONE" and (task / "DONE").is_file() and "DONE" not in names:
        return ""
    return terminal


if __name__ == "__main__":
    if sys.argv[1] == "terminal":
        pids, _ = live_processes(sys.argv[2])
        print(effective_terminal(sys.argv[2], sys.argv[3], pids))
    else:
        pids, _ = live_processes(sys.argv[1])
        print(*pids, sep="\n")
