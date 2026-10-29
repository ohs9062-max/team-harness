#!/usr/bin/env python3
"""Run integration canaries in a disposable home against a candidate checkout."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time

SID = "01a186b5-f3ac-7c62-a02a-3ed039e056bb"


def run(cmd, env, timeout=60):
    return subprocess.run(cmd, env=env, text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("worktree", type=Path)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    root = args.worktree.resolve()
    fixture = root / "src/tests/data/realistic"
    python = root / ".venv/bin/python"
    if not python.exists():
        python = Path(shutil.which("python3") or "python3")
    failed = False

    def check(name, action):
        nonlocal failed
        try:
            ok, detail = action()
        except Exception as exc:
            ok, detail = False, str(exc).replace("\n", " ")[:300]
        print(f"{'PASS' if ok else 'FAIL'} {name}: {str(detail).replace(chr(10), ' ')}", flush=True)
        failed |= not ok

    with tempfile.TemporaryDirectory(prefix="th-canary-") as td:
        base = Path(td)
        home, watch, bin_dir, workspace = [base / n for n in ("home", "watch", "bin", "workspace")]
        for p in (home, watch, bin_dir, workspace):
            p.mkdir()
        suffix = base / "suffix.md"
        suffix.write_text("Synthetic canary worker.\n")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TH_", "CODEX_"))}
        env.update(HOME=str(home), TH_WATCH_DIR=str(watch), TH_WORKSPACE=str(workspace),
                   TH_WORKER_SUFFIX=str(suffix), TH_NO_DAEMON="1", TH_ONCE="1",
                   TEAM_HARNESS_JEV="0", TH_DIGEST_COMMAND="/bin/true",
                   PATH=f"{bin_dir}:{os.environ.get('PATH', '')}", PYTHONPATH=str(root / "src"),
                   CANARY_FIXTURE=str(fixture), CANARY_HOME=str(home))
        (watch / "report.last").write_text(str(time.time()))
        (watch / "archive.last").write_text(str(time.time()))
        fake = bin_dir / "codex"
        fake.write_text('''#!/usr/bin/env python3
import os, pathlib, shutil, signal, sys, time
p = pathlib.Path.cwd()
f = pathlib.Path(os.environ["CANARY_FIXTURE"])
s = pathlib.Path(os.environ["CANARY_HOME"])/".codex/sessions"
s.mkdir(parents=True, exist_ok=True)
mode = (p/"mode").read_text().strip()
sid = {"done": "01a186b5-f3ac-7c62-a02a-3ed039e056bb", "rollover": "01a186b5-f3ac-7c62-a02a-3ed039e056bc", "failed": "01a186b5-f3ac-7c62-a02a-3ed039e056bd"}[mode]
shutil.copyfile(f/"session.jsonl", s/("rollout-" + sid + ".jsonl"))
sys.stdout.write((f/"codex.log").read_text().replace("01a186b5-f3ac-7c62-a02a-3ed039e056bb", sid)); sys.stdout.flush()
count = p/"canary-attempts"
n = int(count.read_text())+1 if count.exists() else 1
count.write_text(str(n))
mode = (p/"mode").read_text().strip()
if mode == "rollover":
    signal.pause()
elif mode == "failed" and n == 1:
    (p/"FAILED").write_text("INCOMPLETE: remaining work")
else:
    (p/"DONE").write_text("complete")
''')
        fake.chmod(0o755)
        notify = bin_dir / "notify-send"
        notify.write_text("#!/bin/sh\nexit 0\n")
        notify.chmod(0o755)
        tasks = []

        def launch(mode):
            task = base / mode
            task.mkdir()
            tasks.append(task)
            (task / "mode").write_text(mode)
            (task / "task.md").write_text(f"---\nkind: code\ntask_key: canary-{mode}\ncontext_budget: 25000\n---\nComplete the synthetic task.\n")
            (task / "WORKLOG.md").write_text("progress\n")
            r = run([str(root / "scripts/th-launch.sh"), str(task), "codex", "gpt-6.1-terra", "medium", str(root)], env)
            if r.returncode:
                raise RuntimeError(f"launch rc={r.returncode}: {r.stdout[-300:]}")
            return task

        def once():
            r = run([str(root / "scripts/th-watchd.sh")], env, 120)
            if r.returncode:
                raise RuntimeError(f"watchd rc={r.returncode}: {r.stdout[-300:]}")

        def events():
            p = watch / "events.log"
            return p.read_text(errors="replace") if p.exists() else ""

        def done():
            task = launch("done")
            time.sleep(1)
            once()
            text = events()
            return ("[완료]" in text and "[멈춤]" not in text, "DONE event and no stall")

        def rollover():
            task = launch("rollover")
            time.sleep(1)
            (task / "rollover_state.json").write_text('{"last_check": 0, "count": 0, "last_hash": null}')
            (task / "WORKLOG.md").write_text("updated progress\n")
            once()
            return ("롤오버(1/3)" in events(), "session measurement and rollover")

        def auto_continue():
            task = launch("failed")
            time.sleep(1)
            once()
            time.sleep(1)
            once()
            count = int((task / "canary-attempts").read_text())
            return ("[자동 계속]" in events() and count >= 2, f"relaunches={count - 1}")

        def registry():
            r = run([str(python), str(fixture / "generate_registry.py"), str(base)], env)
            if r.returncode:
                raise RuntimeError(r.stdout)
            (watch / "tasks.txt").write_text(r.stdout)
            start = time.monotonic()
            once()
            elapsed = time.monotonic() - start
            return (elapsed < 30, f"500 registrations {elapsed:.2f}s (limit 30s)")

        def report():
            shutil.copyfile(fixture / "events.log", watch / "events.log")
            r = run([str(python), "-c", "from team_harness.cli import main; main()", "report"], env)
            return (r.returncode == 0, f"exit {r.returncode}: {r.stdout[-120:].strip()}")

        def brief():
            code = '''from pathlib import Path
from team_harness.brief_check import check_brief_text
import sys
root=Path(sys.argv[1]); bad=[]
for p in sorted((root/"src/tests/data/brief_check").glob("good_*/task.md")):
    bad += [str(p.parent.name)+":"+x.rule for x in check_brief_text(p.read_text(), p.parent) if x.level == "error"]
print(", ".join(bad) if bad else "all good fixtures accepted")
sys.exit(bool(bad))'''
            r = run([str(python), "-c", code, str(root)], env)
            return (r.returncode == 0, r.stdout.strip()[-200:])

        try:
            if not args.live:
                check("brief", brief)
                check("report", report)
                if not args.quick:
                    check("done", done)
                    check("rollover", rollover)
                    check("auto-continue", auto_continue)
                    check("registry", registry)
            else:
                real_watch = Path(os.environ.get("TH_WATCH_DIR", str(Path.home() / ".team-harness/watch")))

                def live_registry():
                    registry_path = real_watch / "tasks.txt"
                    if not registry_path.is_file():
                        return False, "live tasks.txt unavailable"
                    script = '''source "$1/scripts/th-watch-lib.sh"
count=0; pending=0
while IFS= read -r task || [[ -n $task ]]; do
  [[ -n $task && -d $task ]] || continue
  ((count+=1))
  watch_task_settled "$task" || ((pending+=1))
done < "$2/tasks.txt"
printf "%s %s\\n" "$count" "$pending"
'''
                    start = time.monotonic()
                    r = run(["bash", "-c", script, "bash", str(root), str(real_watch)], env, 120)
                    elapsed = time.monotonic() - start
                    if r.returncode:
                        return False, f"live read-only scan failed ({r.returncode})"
                    count, pending = map(int, r.stdout.split())
                    return True, (f"read-only settled scan {elapsed:.2f}s, existing={count}, "
                                  f"unsettled={pending}")

                def live_report():
                    source = real_watch / "events.log"
                    if not source.is_file():
                        return False, "live events.log unavailable"
                    shadow = base / "live-report"
                    shadow.mkdir()
                    (shadow / "events.log").symlink_to(source)
                    report_env = dict(env, TH_WATCH_DIR=str(shadow))
                    r = run([str(python), "-c", "from team_harness.cli import main; main()", "report"], report_env)
                    return r.returncode == 0, f"read-only report exit {r.returncode}"

                check("live registry", live_registry)
                print("SKIP(범위 밖) live full watchd loop", flush=True)
                check("live report", live_report)
        finally:
            def fake_pids():
                found = []
                for entry in Path("/proc").glob("[0-9]*/cmdline"):
                    try:
                        if str(fake).encode() in entry.read_bytes().split(b"\0"):
                            found.append(int(entry.parent.name))
                    except OSError:
                        continue
                return found

            for pid in fake_pids():
                try:
                    os.killpg(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            time.sleep(0.1)
            remaining = fake_pids()
            check("worker cleanup", lambda: (not remaining, f"remaining fake workers: {len(remaining)}"))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
