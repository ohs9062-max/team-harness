#!/usr/bin/env python3
"""Read-only process discovery shared by status and watch."""
import os
import pwd
import re
import sys
import time
from pathlib import Path


_snapshot = None
_snapshot_at = 0.0


def _process_snapshot(proc_entries=None, shared=None):
    """Build watch and job indexes once for this snapshot."""
    global _snapshot, _snapshot_at
    now = time.monotonic()
    if _snapshot is not None and now - _snapshot_at < 1.0:
        return _snapshot
    watches = {}
    def read(proc, name):
        return shared.read(proc, name) if shared is not None else (proc / name).read_bytes()
    def cwd(proc):
        return shared.cwd(proc) if shared is not None else os.readlink(proc / "cwd")
    default_home = pwd.getpwuid(os.getuid()).pw_dir
    for proc in proc_entries if proc_entries is not None else Path("/proc").glob("[0-9]*"):
        try:
            if proc.stat().st_uid != os.getuid():
                continue
            pid = int(proc.name)
            stat = read(proc, "stat").decode().rsplit(") ", 1)[1].split()
            if stat[0] == "Z":
                continue
            if shared is None:
                environ = read(proc, "environ").split(b"\0")
                values = dict(os.fsdecode(item).split("=", 1) for item in environ if b"=" in item)
            else:
                environ, values, _ = shared.environ(proc)
            home = Path(values.get("HOME") or default_home)
            path = values.get("TH_WATCH_DIR", str(home / ".team-harness/watch"))
            if path == "~" or path.startswith("~/"):
                path = str(home) + path[1:]
            candidate = Path(path)
            if not candidate.is_absolute():
                candidate = Path(cwd(proc)) / candidate
            watch = candidate.resolve()
            bucket = watches.setdefault(watch, {"matched": set(), "starts": {}, "tags": {}, "guesses": []})
            bucket["matched"].add(pid)
            bucket["starts"][pid] = stat[19]
            worker_dirs = [item.split(b"=", 1)[1] for item in environ if item.startswith(b"TH_WORKER_OUTPUT_DIR=")]
            try:
                worker = Path(os.fsdecode(worker_dirs[0])).resolve() if worker_dirs else None
            except (OSError, ValueError):
                worker = False
            for item in environ:
                if item.startswith(b"TH_JOB_TASK="):
                    bucket["tags"].setdefault(item[len(b"TH_JOB_TASK="):], []).append((pid, worker))
            if int(stat[3]) == pid:
                try:
                    args = read(proc, "cmdline").split(b"\0")
                    cmd = b" ".join(args).decode(errors="replace")
                    excluded = ("th-status", "th-watch", "th-inbox-wait", "tail", "less", "grep", "vim", "nvim", "emacs", "nano", "code", "claude", "codex", "shell-snapshots")
                    if not any(x in cmd.lower() for x in excluded):
                        shell = os.path.basename(os.fsdecode(args[0])) in ("sh", "bash", "dash", "zsh")
                        bucket["guesses"].append((pid, cmd, shell))
                except (OSError, IndexError):
                    pass
        except (OSError, ValueError, IndexError):
            continue
    _snapshot, _snapshot_at = watches, now
    return watches


def live_processes(task, snapshot=None):
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
    watch = Path(os.environ.get("TH_WATCH_DIR", str(Path.home() / ".team-harness/watch"))).expanduser().resolve()
    bucket = (snapshot if snapshot is not None else _process_snapshot()).get(watch, {})
    direct &= bucket.get("matched", set())
    rejected = set()
    for pid, worker in bucket.get("tags", {}).get(os.fsencode(str(task)), []):
        if worker is False:
            rejected.add(pid)
        elif worker is not None and worker != task:
            direct.discard(pid)
            rejected.add(pid)
        else:
            tagged.add(pid)
    def still_same(pid):
        try:
            fields = Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()
            return fields[0] != "Z" and fields[19] == bucket.get("starts", {}).get(pid)
        except (OSError, IndexError):
            return False

    selected = {pid for pid in direct | tagged if still_same(pid)}
    if selected:
        return sorted(selected), False
    for pid, cmd, shell in bucket.get("guesses", []):
        if pid in rejected:
            continue
        matches = task.name in cmd
        if shell:
            matches = bool(re.search(r"/" + re.escape(task.name) + r"(?:/|\b)", cmd))
        if matches and still_same(pid):
            guessed.add(pid)
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
