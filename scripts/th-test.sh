#!/usr/bin/env bash
# Isolate pytest before plugin discovery and process observation start.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
test_home=$(mktemp -d "${TMPDIR:-/tmp}/th-test-home.XXXXXXXX")
trap 'rm -rf -- "$test_home"' EXIT
export HOME="$test_home" TH_WATCH_DIR="$test_home/watch"
export TH_NO_DAEMON=1
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
unset TH_INBOX_OWNER TH_WORKSPACE CLAUDE_PROJECT_DIR CLAUDE_CODE_SESSION_ID CLAUDE_PID
unset TH_ROLE TH_SESSION_KEY TH_JOB_TASK TH_WORKER_OUTPUT_DIR TH_WORKER_ALLOW_PATHS ORCA_PANE_KEY
cd "$root"
uv run --extra dev "$root/scripts/th-env.sh" -c '
import ctypes, os, runpy, signal, sys, time
from pathlib import Path

for key in tuple(os.environ):
    if key.startswith("TH_INTERNAL_"):
        del os.environ[key]
os.environ.pop("PYTHONNOUSERSITE", None)

# The supervising process adopts orphaned test descendants, not pytest itself.
# It can reap their zombies without changing pytest subprocess wait semantics.
if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
    runpy.run_module("pytest", run_name="__main__")
    raise SystemExit(0)
if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
    raise SystemExit("cannot enable test subreaper")
child = os.fork()
if child == 0:
    runpy.run_module("pytest", run_name="__main__")
    raise SystemExit(0)

# The parent reaps adopted orphans while pytest runs, but never waits for a
# live process and never signals a process group.
status = None
while status is None:
    try:
        pid, code = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        break
    if pid == child:
        status = code
    elif pid == 0:
        time.sleep(0.01)

processes = {}
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit():
        continue
    try:
        stat = (entry / "stat").read_text()
        fields = stat[stat.rfind(")") + 2:].split()
        processes[int(entry.name)] = (int(fields[1]), int(fields[19]))
    except (OSError, ValueError, IndexError):
        continue
owned = {os.getpid()}
while True:
    descendants = {pid for pid, (ppid, _) in processes.items() if ppid in owned}
    if descendants <= owned:
        break
    owned.update(descendants)
for pid in owned - {os.getpid()}:
    try:
        proc = Path("/proc") / str(pid)
        args = (proc / "cmdline").read_bytes().split(b"\0")
        if not any(Path(os.fsdecode(arg)).name == "th-watchd.sh" for arg in args):
            continue
        fd = os.pidfd_open(pid)
        try:
            stat = (proc / "stat").read_text()
            start = int(stat[stat.rfind(")") + 2:].split()[19])
            if start == processes[pid][1]:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
        finally:
            os.close(fd)
    except (OSError, ValueError, IndexError):
        continue
raise SystemExit(os.waitstatus_to_exitcode(status) if status is not None else 1)
' "$@"
