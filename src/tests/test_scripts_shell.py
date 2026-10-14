"""Tests for operational shell scripts (th-launch.sh, th-watch.sh, th-status.sh).

Ensures isolated execution via tmp_path, TH_WATCH_DIR, TH_WORKSPACE,
and mocked worker CLI binaries (codex, agy, claude) on PATH.
"""

from __future__ import annotations

import os
from pathlib import Path
import signal
import stat
import subprocess
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"


def _watchd_pids_for(watch_dir: Path) -> list[int]:
    """Return th-watchd processes whose environment targets ``watch_dir``."""
    expected = f"TH_WATCH_DIR={watch_dir}".encode()
    pids = []
    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        try:
            cmdline = (proc_dir / "cmdline").read_bytes()
            environ = (proc_dir / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if b"th-watchd.sh" in cmdline and expected in environ:
            pids.append(int(proc_dir.name))
    return pids


def _stop_watchd_for(watch_dir: Path) -> None:
    """Stop the session-owning daemon process for one isolated watch directory."""
    for pid in _watchd_pids_for(watch_dir):
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


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
    suffix_file.write_text(
        "## Test worker rules\nFollow instructions.\n", encoding="utf-8"
    )

    # Stub scripts for worker CLIs
    for cli_name in ("codex", "agy", "claude"):
        stub_path = bin_dir / cli_name
        stub_content = f"""#!/usr/bin/env bash
echo "{cli_name} invoked with $@"
sleep 15 &
"""
        stub_path.write_text(stub_content, encoding="utf-8")
        stub_path.chmod(
            stub_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )

    env = dict(os.environ)
    env["TH_WATCH_DIR"] = str(watch_dir)
    env["TH_WORKSPACE"] = str(workspace)
    env["TH_WORKER_SUFFIX"] = str(suffix_file)
    # Individual daemon-start tests opt in; ordinary th-launch tests must not leak one.
    env["TH_NO_DAEMON"] = "1"
    env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

    yield {
        "watch_dir": watch_dir,
        "workspace": workspace,
        "bin_dir": bin_dir,
        "suffix_file": suffix_file,
        "env": env,
        "tmp_path": tmp_path,
    }

    _stop_watchd_for(watch_dir)


def _create_task_dir(
    parent: Path, name: str = "task_1", title: str = "Test Task"
) -> Path:
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


def test_th_launch_passes_sol_model_and_effort_to_codex(test_env: dict):
    """th-launch forwards explicit Sol model and effort without invoking real Codex."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_sol_arguments")
    launch_script = SCRIPTS_DIR / "th-launch.sh"
    captured_args = test_env["tmp_path"] / "codex-args.txt"
    codex_stub = test_env["bin_dir"] / "codex"
    codex_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{captured_args}"\n',
        encoding="utf-8",
    )
    codex_stub.chmod(codex_stub.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        [
            str(launch_script),
            str(task_dir),
            "codex",
            "gpt-6.1-sol",
            "medium",
            str(task_dir),
        ],
        env=test_env["env"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    time.sleep(0.1)
    assert captured_args.read_text(encoding="utf-8").splitlines()[:5] == [
        "exec",
        "-m",
        "gpt-6.1-sol",
        "-c",
        "model_reasoning_effort=medium",
    ]


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
    res = subprocess.run(
        [str(watch_script), "1"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    # Should not notify for other workspace
    assert str(task_other) not in res.stdout
    # task_other should remain in tasks.txt
    assert str(task_other) in tasks_txt.read_text(encoding="utf-8")

    # Now add task_my
    tasks_txt.write_text(f"{task_other}\n{task_my}\n", encoding="utf-8")
    res2 = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
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
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
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
    res = subprocess.run(
        [str(watch_script), "1"], env=env, capture_output=True, text=True
    )
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
                subprocess.run, [str(helper)] + [str(t) for t in chunk], check=True
            )
            for chunk in chunks
        ]
        for f in futures:
            f.result()

    lines = [
        line.strip()
        for line in registry.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == tasks_count
    for t in task_dirs:
        assert str(t) in lines


def test_th_launch_concurrent_registry_integrity(test_env: dict):
    """ops-02: Concurrently execute th-launch.sh and verify all tasks exist in tasks.txt."""
    launch_script = SCRIPTS_DIR / "th-launch.sh"
    tasks_count = 10
    task_dirs = [
        _create_task_dir(test_env["tmp_path"], f"conc_launch_{i}")
        for i in range(tasks_count)
    ]

    import concurrent.futures

    def launch_one(t: Path):
        cmd = [str(launch_script), str(t), "codex", "gpt-5.6-terra", "medium", str(t)]
        res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
        assert res.returncode == 0, f"launch failed: {res.stderr}"

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        list(executor.map(launch_one, task_dirs))

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    lines = [
        line.strip()
        for line in tasks_txt.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for t in task_dirs:
        assert str(t) in lines


def test_th_watch_quota_error_removes_task(test_env: dict):
    """ops-05: Quota error removes task from registry tasks.txt."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_quota_rm")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "codex.log").write_text("ERROR: 429 quota exceeded\n", encoding="utf-8")

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "워커 오류(쿼터·용량)" in res.stdout

    # Must be removed from registry
    assert str(task_dir) not in tasks_txt.read_text(encoding="utf-8")


def test_th_watch_dead_process_removes_task(test_env: dict):
    """ops-05: Missing/dead process without DONE/FAILED removes task from registry."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_dead_rm")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "worker.pid").write_text("999999\n", encoding="utf-8")

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "중단 — 실행 중인 프로세스 없음" in res.stdout

    # Must be removed from registry
    assert str(task_dir) not in tasks_txt.read_text(encoding="utf-8")


def test_th_watch_stall_retains_task_in_registry(test_env: dict):
    """ops-05: Stalled task (alive process, no changes) must remain in registry."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_stall_keep")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")

    # Start a live background sleep process
    proc = subprocess.Popen(["sleep", "30"])
    try:
        (task_dir / "worker.pid").write_text(f"{proc.pid}\n", encoding="utf-8")

        # Fake existing signature state file older than TH_STALL_SEC
        state_file = task_dir / ".th_sig"
        # 10 ticks, older than 10 seconds ago
        state_file.write_text("10:12345|1000000000\n", encoding="utf-8")

        tasks_txt = test_env["watch_dir"] / "tasks.txt"
        tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

        watch_script = SCRIPTS_DIR / "th-watch.sh"
        env = dict(test_env["env"])
        env["TH_POLL_SEC"] = "1"
        env["TH_STALL_SEC"] = "1"  # stall threshold 1s
        res = subprocess.run(
            [str(watch_script), "2"], env=env, capture_output=True, text=True
        )
        assert res.returncode == 0
        assert "멈춤" in res.stdout

        # Registry must RETAIN the task
        assert str(task_dir) in tasks_txt.read_text(encoding="utf-8")
    finally:
        proc.terminate()
        proc.wait()


def test_th_status_worker_pid_alive_shows_running(test_env: dict):
    """ops-03: th-status shows '진행' if worker_pid is alive even if job_pid is dead."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_status_alive")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")

    proc = subprocess.Popen(["sleep", "30"])
    try:
        (task_dir / "job.pid").write_text("999999\n", encoding="utf-8")
        (task_dir / "worker.pid").write_text(f"{proc.pid}\n", encoding="utf-8")

        tasks_txt = test_env["watch_dir"] / "tasks.txt"
        tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

        status_script = SCRIPTS_DIR / "th-status.sh"
        res = subprocess.run(
            [str(status_script)], env=test_env["env"], capture_output=True, text=True
        )
        assert res.returncode == 0
        assert "진행" in res.stdout
        assert "중단?" not in res.stdout
    finally:
        proc.terminate()
        proc.wait()


def test_th_status_both_pids_dead_shows_stopped(test_env: dict):
    """ops-03: th-status shows '중단?' if both job_pid and worker_pid are dead."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_status_dead")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "job.pid").write_text("999998\n", encoding="utf-8")
    (task_dir / "worker.pid").write_text("999999\n", encoding="utf-8")

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    status_script = SCRIPTS_DIR / "th-status.sh"
    res = subprocess.run(
        [str(status_script)], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "중단?" in res.stdout


def test_th_launch_claude_worker(test_env: dict):
    """ops-04: th-launch correctly launches claude worker with flags and writes claude.log."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_launch_claude")
    launch_script = SCRIPTS_DIR / "th-launch.sh"

    cmd = [
        str(launch_script),
        str(task_dir),
        "claude",
        "claude-sonnet-5",
        "high",
        str(task_dir),
    ]
    res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
    assert res.returncode == 0, f"launch failed: {res.stderr}\n{res.stdout}"
    assert "launched [claude]" in res.stdout

    claude_log = task_dir / "claude.log"
    deadline = time.time() + 3
    log_content = ""
    while time.time() < deadline:
        if claude_log.is_file():
            log_content = claude_log.read_text(encoding="utf-8")
            if "claude invoked with" in log_content:
                break
        time.sleep(0.1)
    assert "claude invoked with" in log_content
    assert (
        "-p --dangerously-skip-permissions --model claude-sonnet-5 --strict-mcp-config"
        in log_content
    )


def test_th_watch_recognizes_claude_log_quota(test_env: dict):
    """ops-04: th-watch recognizes quota/rate-limit error in claude.log."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_claude_quota")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "claude.log").write_text(
        "ERROR: 429 rate limit exceeded\n", encoding="utf-8"
    )

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "워커 오류(쿼터·용량)" in res.stdout
    assert "새 claude 실행 잠금" in res.stdout

    quota_lock = test_env["watch_dir"] / "quota_lock.claude"
    assert quota_lock.is_file(), "quota_lock.claude must be created"


def test_th_status_recognizes_claude_log_quota(test_env: dict):
    """ops-04: th-status recognizes quota error in claude.log."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_claude_status_quota")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "claude.log").write_text(
        "ERROR: 429 rate limit reached\n", encoding="utf-8"
    )

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_dir}\n", encoding="utf-8")

    status_script = SCRIPTS_DIR / "th-status.sh"
    res = subprocess.run(
        [str(status_script)], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "워커 오" in res.stdout


def test_th_watch_cpu_ticks_includes_grandchild_processes():
    """ops-14: cpu_ticks counts CPU used by a descendant outside the root session."""
    root = subprocess.Popen(
        ["bash", "-c", "setsid bash -c 'yes > /dev/null & wait' & wait"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    child_pid = None
    hog_pid = None
    try:
        time.sleep(0.2)
        child_pid = int(
            subprocess.check_output(
                ["pgrep", "-P", str(root.pid)], text=True
            ).splitlines()[0]
        )
        hog_pid = int(
            subprocess.check_output(
                ["pgrep", "-P", str(child_pid)], text=True
            ).splitlines()[0]
        )
        hog_ticks = int(
            Path(f"/proc/{hog_pid}/stat").read_text().split(") ", 1)[1].split()[11]
        )

        command = 'source <(sed -n \'/^cpu_ticks() {/,/^}$/p\' "$1"); cpu_ticks "$2"'
        result = subprocess.run(
            [
                "bash",
                "-c",
                command,
                "bash",
                str(SCRIPTS_DIR / "th-watch.sh"),
                str(root.pid),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert int(result.stdout.strip()) >= hog_ticks
    finally:
        for pid in (hog_pid, child_pid, root.pid):
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        root.wait(timeout=5)


def test_th_watch_failed_reason_preserves_utf8_boundaries(test_env: dict):
    """ops-08: FAILED notifications stay decodable when the reason exceeds 200 bytes."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_utf8_failed")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "FAILED").write_text("가" * 70, encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    result = subprocess.run(
        [str(SCRIPTS_DIR / "th-watch.sh"), "2"],
        env=env,
        check=True,
        capture_output=True,
    )

    output = result.stdout.decode("utf-8")
    assert "실패" in output
    assert "가" in output


def test_th_watch_signature_ignores_deep_files(test_env: dict):
    """ops-09: watcher signatures inspect only operational files within three levels."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_bounded_signature")
    status_file = task_dir / "STATUS"
    status_file.write_text("working\n", encoding="utf-8")
    deep_log = task_dir / "one" / "two" / "three" / "deep.log"
    deep_log.parent.mkdir(parents=True)
    deep_log.write_text("large dataset worker output\n", encoding="utf-8")
    os.utime(task_dir / "task.md", (50, 50))
    os.utime(status_file, (100, 100))
    os.utime(deep_log, (200, 200))

    command = (
        "source <(sed -n '/^latest_signature() {/,/^}$/p' \"$1\"); "
        'latest_signature "$2"'
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            command,
            "bash",
            str(SCRIPTS_DIR / "th-watch.sh"),
            str(task_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == "100"


def test_th_launch_reads_each_agy_cmdline_once():
    """ops-15: agy reparenting detection parses each /proc cmdline from one read."""
    launch_source = (SCRIPTS_DIR / "th-launch.sh").read_text(encoding="utf-8")
    agy_search = launch_source.split("for p in /proc/[0-9]*/cmdline; do", 1)[1].split(
        "done", 1
    )[0]

    assert "IFS= read -r -d '' exe < \"$p\"" not in agy_search
    assert agy_search.count('< "$p"') == 1


def test_th_watch_skips_owner_inference_without_work_logs(test_env: dict):
    """ops-25: an empty work_log glob is not forwarded as a literal grep file."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_without_owner")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")
    grep_args = test_env["tmp_path"] / "grep-args.txt"
    grep_stub = test_env["bin_dir"] / "grep"
    grep_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{grep_args}"\nexec /usr/bin/grep "$@"\n',
        encoding="utf-8",
    )
    grep_stub.chmod(grep_stub.stat().st_mode | stat.S_IEXEC)

    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    subprocess.run(
        [str(SCRIPTS_DIR / "th-watch.sh"), "1"],
        env=env,
        check=True,
        capture_output=True,
    )

    assert not grep_args.exists()


def test_th_launch_extracts_verify_sh(test_env: dict):
    """B. 보조 검수: task.md의 ## 검수 블록을 verify.sh로 저장하고 .th_worktree를 기록."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_extract")
    task_md = task_dir / "task.md"
    task_md.write_text(
        "# Test Task\nSome description.\n\n## 검수\n```bash\npytest -q\n```\n",
        encoding="utf-8",
    )
    worktree_dir = test_env["tmp_path"] / "fake_worktree"
    worktree_dir.mkdir(parents=True, exist_ok=True)

    launch_script = SCRIPTS_DIR / "th-launch.sh"
    cmd = [
        str(launch_script),
        str(task_dir),
        "codex",
        "gpt-5.6-terra",
        "medium",
        str(worktree_dir),
    ]
    res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
    assert res.returncode == 0, f"launch failed: {res.stderr}\n{res.stdout}"

    verify_sh = task_dir / "verify.sh"
    assert verify_sh.is_file(), "verify.sh must be extracted from task.md"
    assert "pytest -q" in verify_sh.read_text(encoding="utf-8")
    assert bool(verify_sh.stat().st_mode & stat.S_IXUSR), "verify.sh must be executable"

    worktree_file = task_dir / ".th_worktree"
    assert worktree_file.is_file()
    assert worktree_file.read_text(encoding="utf-8").strip() == str(worktree_dir)


def test_th_launch_warns_when_no_verify_block(test_env: dict):
    """B. 보조 검수: ## 검수 블록이 없으면 경고만 하고 정상 진행."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_no_verify")
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
    assert res.returncode == 0
    assert not (task_dir / "verify.sh").exists()
    assert "경고" in res.stderr or "warning" in res.stderr.lower()


def test_th_watch_runs_verify_success(test_env: dict):
    """B. 보조 검수: DONE 시 verify.sh 성공하면 VERIFIED 생성 및 알림 문구에 검수 통과 반영."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_ok")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / ".th_worktree").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")

    verify_sh = task_dir / "verify.sh"
    verify_sh.write_text(
        "#!/usr/bin/env bash\necho 'tests passed'\nexit 0\n", encoding="utf-8"
    )
    verify_sh.chmod(verify_sh.stat().st_mode | stat.S_IEXEC)

    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "완료" in res.stdout
    assert "검수 통과" in res.stdout
    assert (task_dir / "VERIFIED").is_file()
    assert not (task_dir / "VERIFY_FAILED").exists()


def test_th_watch_runs_verify_failure(test_env: dict):
    """B. 보조 검수: DONE 시 verify.sh 실패하면 VERIFY_FAILED 생성 및 알림 문구에 검수 실패 반영."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_fail")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / ".th_worktree").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")

    verify_sh = task_dir / "verify.sh"
    verify_sh.write_text(
        "#!/usr/bin/env bash\necho 'assertion error'\nexit 1\n", encoding="utf-8"
    )
    verify_sh.chmod(verify_sh.stat().st_mode | stat.S_IEXEC)

    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "완료" in res.stdout
    assert "검수 실패" in res.stdout
    assert (task_dir / "VERIFY_FAILED").is_file()
    content = (task_dir / "VERIFY_FAILED").read_text(encoding="utf-8")
    assert "assertion error" in content


def test_th_watch_runs_verify_none(test_env: dict):
    """B. 보조 검수: verify.sh가 없으면 알림 문구에 검수 없음 반영."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_none")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "완료" in res.stdout
    assert "검수 없음" in res.stdout


def test_th_watch_verify_common_checks_root_md(test_env: dict):
    """B. 보조 검수 공통 검사: 저장소 루트에 새 *.md/RESULTS.md/WORKLOG.md 생성 시 VERIFY_FAILED."""
    repo_dir = test_env["tmp_path"] / "git_repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo_dir), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
    )

    # Place untracked RESULTS.md at repository root
    (repo_dir / "RESULTS.md").write_text("leak at root\n", encoding="utf-8")

    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_leak")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / ".th_worktree").write_text(str(repo_dir), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")

    verify_sh = task_dir / "verify.sh"
    verify_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    verify_sh.chmod(verify_sh.stat().st_mode | stat.S_IEXEC)

    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "검수 실패" in res.stdout
    assert (task_dir / "VERIFY_FAILED").is_file()
    content = (task_dir / "VERIFY_FAILED").read_text(encoding="utf-8")
    assert "RESULTS.md" in content


def test_th_watch_verify_uv_lock_warning_not_failure(test_env: dict):
    """B. 보조 검수 공통 검사: uv.lock 변경은 금지가 아니라 '검토 필요' 경고로 VERIFIED 처리."""
    repo_dir = test_env["tmp_path"] / "git_repo_lock"
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-b", "main", str(repo_dir)], check=True, capture_output=True
    )
    (repo_dir / "uv.lock").write_text("lock v1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_dir), "add", "uv.lock"], check=True)
    subprocess.run(["git", "-C", str(repo_dir), "commit", "-m", "add lock"], check=True)

    # Modify uv.lock
    (repo_dir / "uv.lock").write_text("lock v2 modified\n", encoding="utf-8")

    task_dir = _create_task_dir(test_env["tmp_path"], "task_verify_uv_lock")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / ".th_worktree").write_text(str(repo_dir), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")

    verify_sh = task_dir / "verify.sh"
    verify_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    verify_sh.chmod(verify_sh.stat().st_mode | stat.S_IEXEC)

    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "검수 통과" in res.stdout
    assert (task_dir / "VERIFIED").is_file()
    content = (task_dir / "VERIFIED").read_text(encoding="utf-8")
    assert "uv.lock" in content
    assert "검토 필요" in content or "warning" in content.lower()


def test_th_status_shows_verify_column(test_env: dict):
    """B. 보조 검수: th-status.sh에 '검수' 열이 추가되고 상태가 표시됨."""
    task_ok = _create_task_dir(test_env["tmp_path"], "status_task_ok")
    (task_ok / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_ok / "DONE").write_text("Done\n", encoding="utf-8")
    (task_ok / "VERIFIED").write_text("All passed\n", encoding="utf-8")

    task_fail = _create_task_dir(test_env["tmp_path"], "status_task_fail")
    (task_fail / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_fail / "DONE").write_text("Done\n", encoding="utf-8")
    (task_fail / "VERIFY_FAILED").write_text("Failed\n", encoding="utf-8")

    task_none = _create_task_dir(test_env["tmp_path"], "status_task_none")
    (task_none / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_none / "DONE").write_text("Done\n", encoding="utf-8")

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text(f"{task_ok}\n{task_fail}\n{task_none}\n", encoding="utf-8")

    status_script = SCRIPTS_DIR / "th-status.sh"
    res = subprocess.run(
        [str(status_script)], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "검수" in res.stdout
    assert "통과" in res.stdout
    assert "실패" in res.stdout


def test_th_watchd_notifies_inbox_and_events_log(test_env: dict):
    """A. 상시 감시 데몬: inbox.md에 기록, events.log에 기록, 같은 사건 1회만."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_daemon_done")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watchd_script = SCRIPTS_DIR / "th-watchd.sh"
    env = dict(test_env["env"])
    env["TH_POLL_SEC"] = "1"
    env["TH_ONCE"] = "1"  # Test flag: run one loop iteration then exit

    res = subprocess.run([str(watchd_script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0

    inbox_file = test_env["workspace"] / ".claude" / "inbox.md"
    assert inbox_file.is_file(), "inbox.md must be created in owner workspace"
    inbox_content = inbox_file.read_text(encoding="utf-8").strip()
    # verify format: time, task, event, summary, verify
    assert "task_daemon_done" in inbox_content
    assert "완료" in inbox_content
    assert "검수 없음" in inbox_content or "없음" in inbox_content

    events_log = test_env["watch_dir"] / "events.log"
    assert events_log.is_file()
    assert "task_daemon_done" in events_log.read_text(encoding="utf-8")

    # Second run: must not duplicate inbox line
    res2 = subprocess.run([str(watchd_script)], env=env, capture_output=True, text=True)
    assert res2.returncode == 0
    lines = [
        line
        for line in inbox_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(lines) == 1, "Same event must be recorded only once"


def test_th_watchd_notify_send_desktop(test_env: dict):
    """A. 상시 감시 데몬: notify-send가 있으면 데스크톱 알림, 없으면 건너뜀."""
    notify_log = test_env["tmp_path"] / "notify.log"
    notify_stub = test_env["bin_dir"] / "notify-send"
    notify_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{notify_log}"\n',
        encoding="utf-8",
    )
    notify_stub.chmod(notify_stub.stat().st_mode | stat.S_IEXEC)

    task_dir = _create_task_dir(test_env["tmp_path"], "task_daemon_notify")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watchd_script = SCRIPTS_DIR / "th-watchd.sh"
    env = dict(test_env["env"])
    env["TH_ONCE"] = "1"
    res = subprocess.run([str(watchd_script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert notify_log.is_file(), "notify-send must be invoked"
    assert "task_daemon_notify" in notify_log.read_text(encoding="utf-8")


def test_th_watchd_and_th_watch_marker_separation(test_env: dict):
    """A. 상시 감시 데몬과 th-watch의 표시 파일 분리 (.th_daemon.* vs .th_notified.*)."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_separation")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Completed\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watchd_script = SCRIPTS_DIR / "th-watchd.sh"
    env = dict(test_env["env"])
    env["TH_ONCE"] = "1"
    res_d = subprocess.run(
        [str(watchd_script)], env=env, capture_output=True, text=True
    )
    assert res_d.returncode == 0
    assert (task_dir / ".th_daemon.done").is_file(), "Daemon uses .th_daemon.* marker"
    assert not (task_dir / ".th_notified.done").exists(), "th-watch marker untouched"

    # Now run th-watch.sh; it should still detect DONE because .th_notified.done is absent
    watch_script = SCRIPTS_DIR / "th-watch.sh"
    env["TH_POLL_SEC"] = "1"
    res_w = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res_w.returncode == 0
    assert "완료" in res_w.stdout
    assert (task_dir / ".th_notified.done").is_file()


def test_th_watchd_single_instance_flock(test_env: dict):
    """A. 상시 감시 데몬은 flock으로 단일 인스턴스만 실행."""
    watch_dir = test_env["watch_dir"]
    lock_file = watch_dir / "watchd.lock"

    # Hold the lock in a background process
    proc = subprocess.Popen(
        ["bash", "-c", f"exec 201>'{lock_file}'; flock 201; sleep 10"]
    )
    try:
        time.sleep(0.2)
        watchd_script = SCRIPTS_DIR / "th-watchd.sh"
        env = dict(test_env["env"])
        env["TH_ONCE"] = "1"
        res = subprocess.run(
            [str(watchd_script)], env=env, capture_output=True, text=True
        )
        assert res.returncode == 0, "Second instance should exit cleanly"
        assert "already running" in res.stderr.lower() or res.stdout == ""
    finally:
        proc.terminate()
        proc.wait()


def test_th_launch_starts_daemon_if_not_running(test_env: dict):
    """A. Relative th-launch invocation starts a daemon from the repository cwd."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_launch_daemon")

    cmd = [
        "bash",
        "scripts/th-launch.sh",
        str(task_dir),
        "codex",
        "gpt-5.6-terra",
        "medium",
        str(task_dir),
    ]
    env = dict(test_env["env"])
    env.pop("TH_NO_DAEMON")
    res = subprocess.run(cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    assert res.returncode == 0

    lock_file = test_env["watch_dir"] / "watchd.lock"
    assert lock_file.is_file(), (
        "Daemon lock file should be touched/created by daemon launch"
    )
    assert _watchd_pids_for(test_env["watch_dir"]), "Daemon should be running"


def test_watch_cli_completed_verified_records_jev_decision(test_env: dict):
    """C. Jev 관찰: 완료+검수통과 시 DecisionRouter로 판단 후 jev_decisions.log 기록."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_jev_done")
    (task_dir / "STATUS").write_text(
        "[2026-10-13 14:00] Step 1\n[2026-10-13 14:05] Step 2\n", encoding="utf-8"
    )
    (task_dir / "WORKLOG.md").write_text("## Goal\nComplete task\n", encoding="utf-8")
    (task_dir / "VERIFIED").write_text("=== 검수 통과 ===\n", encoding="utf-8")

    watch_dir = test_env["watch_dir"]
    jev_log = watch_dir / "jev_decisions.log"

    env = dict(test_env["env"])
    env["TYPESAFE_API_KEY"] = "fake-key-for-test"
    cmd = [
        "python3",
        "-m",
        "team_harness.decisions.watch_cli",
        str(task_dir),
        "완료",
        str(watch_dir),
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert jev_log.is_file(), "jev_decisions.log must be created"
    log_content = jev_log.read_text(encoding="utf-8")
    assert "task_jev_done" in log_content
    assert "완료" in log_content
    assert "choice=" in log_content


def test_watch_cli_stalled_records_jev_decision(test_env: dict):
    """C. Jev 관찰: 멈춤 시 WAIT_MORE, KILL, NOTIFY 선택지 중 판단."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_jev_stall")
    (task_dir / "STATUS").write_text("Working\n", encoding="utf-8")
    (task_dir / "WORKLOG.md").write_text("Long operation\n", encoding="utf-8")

    watch_dir = test_env["watch_dir"]
    jev_log = watch_dir / "jev_decisions.log"

    env = dict(test_env["env"])
    env["TYPESAFE_API_KEY"] = "fake-key"
    cmd = [
        "python3",
        "-m",
        "team_harness.decisions.watch_cli",
        str(task_dir),
        "멈춤",
        str(watch_dir),
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert jev_log.is_file()
    log_content = jev_log.read_text(encoding="utf-8")
    assert "task_jev_stall" in log_content
    assert "멈춤" in log_content


def test_watch_cli_skips_unsupported_events(test_env: dict):
    """C. Jev 관찰: 그 외 사건(실패 등)은 호출/기록하지 않고 스킵."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_jev_fail")
    (task_dir / "FAILED").write_text("Some error\n", encoding="utf-8")

    watch_dir = test_env["watch_dir"]
    jev_log = watch_dir / "jev_decisions.log"
    if jev_log.exists():
        jev_log.unlink()

    env = dict(test_env["env"])
    cmd = [
        "python3",
        "-m",
        "team_harness.decisions.watch_cli",
        str(task_dir),
        "실패",
        str(watch_dir),
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert not jev_log.exists(), "Unsupported events should not produce decisions"


def test_watch_cli_failure_does_not_break_daemon(test_env: dict):
    """C. Jev 관찰: Jev 관찰 실패(API 키 없음/에러 등) 시에도 감시 지속."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_daemon_jev_fail")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Done\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    watchd_script = SCRIPTS_DIR / "th-watchd.sh"
    env = dict(test_env["env"])
    env["TH_ONCE"] = "1"
    env["TYPESAFE_API_KEY"] = ""
    env.pop("JEV_API_KEY", None)

    res = subprocess.run([str(watchd_script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0

    inbox_file = test_env["workspace"] / ".claude" / "inbox.md"
    assert inbox_file.is_file(), "Daemon must still record to inbox despite Jev errors"


def test_watch_cli_quota_records_jev_decision(test_env: dict):
    """C. Jev 관찰: 쿼터 시 LOCK_AND_WAIT, NOTIFY 선택지 중 판단."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_jev_quota")
    (task_dir / "STATUS").write_text("Quota error\n", encoding="utf-8")

    watch_dir = test_env["watch_dir"]
    jev_log = watch_dir / "jev_decisions.log"
    if jev_log.exists():
        jev_log.unlink()

    env = dict(test_env["env"])
    env["TYPESAFE_API_KEY"] = "fake-key"
    cmd = [
        "python3",
        "-m",
        "team_harness.decisions.watch_cli",
        str(task_dir),
        "워커 오류(쿼터·용량)",
        str(watch_dir),
    ]
    res = subprocess.run(cmd, env=env, capture_output=True, text=True)
    assert res.returncode == 0
    assert jev_log.is_file()
    log_content = jev_log.read_text(encoding="utf-8")
    assert "task_jev_quota" in log_content
    assert "choice=" in log_content


def test_th_watchd_jev_watch_off(test_env: dict):
    """C. Jev 관찰: TH_JEV_WATCH=off 설정 시 Jev 결정을 기록하지 않음."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_daemon_jev_off")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "DONE").write_text("Done\n", encoding="utf-8")
    (task_dir / "VERIFIED").write_text("Passed\n", encoding="utf-8")
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    jev_log = test_env["watch_dir"] / "jev_decisions.log"
    if jev_log.exists():
        jev_log.unlink()

    watchd_script = SCRIPTS_DIR / "th-watchd.sh"
    env = dict(test_env["env"])
    env["TH_ONCE"] = "1"
    env["TH_JEV_WATCH"] = "off"
    env["TYPESAFE_API_KEY"] = "fake-key"

    res = subprocess.run([str(watchd_script)], env=env, capture_output=True, text=True)
    assert res.returncode == 0

    assert not jev_log.exists(), (
        "jev_decisions.log must not be written when TH_JEV_WATCH=off"
    )
