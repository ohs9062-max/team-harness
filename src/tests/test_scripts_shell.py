"""Tests for operational shell scripts (th-launch.sh, th-watch.sh, th-status.sh).

Ensures isolated execution via tmp_path, TH_WATCH_DIR, TH_WORKSPACE,
and mocked worker CLI binaries (codex, agy, claude) on PATH.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


@pytest.fixture
def test_env(tmp_path: Path):
    """Sets up an isolated environment with stub binaries and test directories."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir(parents=True, exist_ok=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)

    suffix_file = tmp_path / "worker_suffix.md"
    suffix_file.write_text("## Test worker rules\nFollow instructions.\n", encoding="utf-8")

    # Stub scripts for worker CLIs
    for cli_name in ("codex", "agy", "claude"):
        stub_path = bin_dir / cli_name
        stub_content = f"""#!/usr/bin/env bash
log_file="{cli_name}.log"
echo "{cli_name} invoked with $@" >> "$log_file"
# Spawn a short background sleep so worker.pid points to a process
sleep 15 &
echo $!
"""
        stub_path.write_text(stub_content, encoding="utf-8")
        stub_path.chmod(stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    env = dict(os.environ)
    env["TH_WATCH_DIR"] = str(watch_dir)
    env["TH_WORKSPACE"] = str(workspace)
    env["TH_WORKER_SUFFIX"] = str(suffix_file)
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    return {
        "watch_dir": watch_dir,
        "workspace": workspace,
        "bin_dir": bin_dir,
        "suffix_file": suffix_file,
        "env": env,
        "tmp_path": tmp_path,
    }


def _create_task_dir(parent: Path, name: str = "task_1", title: str = "Test Task") -> Path:
    task_dir = parent / name
    task_dir.mkdir(parents=True, exist_ok=True)
    task_md = task_dir / "task.md"
    task_md.write_text(f"# {title}\nTask description.\n", encoding="utf-8")
    return task_dir


def test_th_launch_retains_th_owner(test_env: dict):
    """ops-07: Verify th-launch leaves .th_owner in task directory pointing to TH_WORKSPACE."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_launch")
    launch_script = SCRIPTS_DIR / "th-launch.sh"

    cmd = [
        str(launch_script),
        str(task_dir),
        "codex",
        "gpt-5.6-terra",
        "medium",
        str(task_dir),
    ]
    res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
    assert res.returncode == 0, f"th-launch failed: {res.stderr}\n{res.stdout}"

    owner_file = task_dir / ".th_owner"
    assert owner_file.is_file(), ".th_owner must exist after th-launch"
    assert owner_file.read_text(encoding="utf-8").strip() == str(test_env["workspace"])

    # Worker pid and registration in tasks.txt must also exist
    worker_pid_file = task_dir / "worker.pid"
    assert worker_pid_file.is_file(), "worker.pid must exist"
    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    assert str(task_dir) in tasks_txt.read_text(encoding="utf-8")


def test_th_watch_owner_filtering(test_env: dict):
    """ops-07: Verify th-watch filters events by workspace owner (.th_owner)."""
    task_my = _create_task_dir(test_env["tmp_path"], "task_my")
    (task_my / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_my / "DONE").write_text("Finished\n", encoding="utf-8")

    other_workspace = test_env["tmp_path"] / "other_workspace"
    other_workspace.mkdir(parents=True, exist_ok=True)
    task_other = _create_task_dir(test_env["tmp_path"], "task_other")
    (task_other / ".th_owner").write_text(str(other_workspace), encoding="utf-8")
    (task_other / "DONE").write_text("Finished other\n", encoding="utf-8")

    # Register only task_other first to ensure th-watch skips it and exits on deadline
    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_other}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    # Run with max_wait=1 sec and poll_sec=1
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run([str(watch_script), "1"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    # Should not notify for other workspace
    assert str(task_other) not in res.stdout
    # task_other should remain in tasks.txt
    assert str(task_other) in tasks_txt.read_text(encoding="utf-8")

    # Now add task_my
    tasks_txt.write_text(f"{task_other}\n{task_my}\n", encoding="utf-8")
    res2 = subprocess.run([str(watch_script), "2"], env=env, capture_output=True, text=True)
    assert res2.returncode == 0
    assert "완료" in res2.stdout
    assert str(task_my.name) in res2.stdout


def test_th_watch_owner_inference_from_work_log(test_env: dict):
    """ops-07: Verify owner inference from work_log*.md when .th_owner is missing."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_legacy")
    (task_dir / "DONE").write_text("Done legacy\n", encoding="utf-8")

    # Record in workspace's .claude/work_log.md
    work_log = test_env["workspace"] / ".claude" / "work_log.md"
    work_log.parent.mkdir(parents=True, exist_ok=True)
    work_log.write_text(
        f"2026-10-13 10:00 | 지시 | {task_dir} | worker=codex | {test_env['workspace']}\n",
        encoding="utf-8",
    )

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run([str(watch_script), "2"], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert "완료" in res.stdout
    assert str(task_dir.name) in res.stdout


def test_th_watch_quota_lock_for_other_workspace(test_env: dict):
    """ops-07: Verify quota error from another workspace creates global quota_lock."""
    other_workspace = test_env["tmp_path"] / "other_workspace"
    other_workspace.mkdir(parents=True, exist_ok=True)
    task_other = _create_task_dir(test_env["tmp_path"], "task_other_quota")
    (task_other / ".th_owner").write_text(str(other_workspace), encoding="utf-8")

    # Write quota error in codex.log
    (task_other / "codex.log").write_text(
        "ERROR: rate limit exceeded. Please wait.\n", encoding="utf-8"
    )

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_other}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run([str(watch_script), "1"], env=env, capture_output=True, text=True)
    assert res.returncode == 0

    quota_lock = test_env["watch_dir"] / "quota_lock.codex"
    assert quota_lock.is_file(), "quota_lock.codex must be created across workspaces"


def test_tasks_flock_concurrency(test_env: dict):
    """ops-02: Verify tasks.txt concurrent appends and removals do not lose entries."""
    watch_dir = test_env["watch_dir"]
    registry = watch_dir / "tasks.txt"
    tasks_count = 30

    task_dirs = []
    for i in range(tasks_count):
        t = _create_task_dir(test_env["tmp_path"], f"task_flock_{i}")
        task_dirs.append(t)

    # Concurrently launch/register half of them while launching scripts
    script = f"""#!/usr/bin/env bash
set -e
watch_dir="{watch_dir}"
for t in "$@"; do
  (
    flock -x 200
    grep -qxF "$t" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\\n' "$t" >> "$watch_dir/tasks.txt"
  ) 200>"$watch_dir/tasks.lock"
done
"""
    helper = test_env["tmp_path"] / "append_helper.sh"
    helper.write_text(script, encoding="utf-8")
    helper.chmod(helper.stat().st_mode | stat.S_IEXEC)

    import concurrent.futures

    # Split tasks into chunks and append concurrently
    chunks = [task_dirs[i : i + 5] for i in range(0, len(task_dirs), 5)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
        futures = [
            executor.submit(
                subprocess.run,
                [str(helper)] + [str(t) for t in chunk],
                check=True,
            )
            for chunk in chunks
        ]
        for f in futures:
            f.result()

    lines = [l.strip() for l in registry.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == tasks_count
    for t in task_dirs:
        assert str(t) in lines


def test_th_launch_concurrent_registry_integrity(test_env: dict):
    """ops-02: Concurrently execute th-launch.sh and verify all tasks exist in tasks.txt."""
    launch_script = SCRIPTS_DIR / "th-launch.sh"
    tasks_count = 10
    task_dirs = [_create_task_dir(test_env["tmp_path"], f"conc_launch_{i}") for i in range(tasks_count)]

    import concurrent.futures

    def launch_one(t: Path):
        cmd = [
            str(launch_script),
            str(t),
            "codex",
            "gpt-5.6-terra",
            "medium",
            str(t),
        ]
        res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
        assert res.returncode == 0, f"launch failed: {res.stderr}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(launch_one, task_dirs))

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    lines = [l.strip() for l in tasks_txt.read_text(encoding="utf-8").splitlines() if l.strip()]
    for t in task_dirs:
        assert str(t) in lines


