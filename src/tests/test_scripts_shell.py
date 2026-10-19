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
    env["TH_DIGEST_COMMAND"] = "/bin/true"
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
    for stale in (
        "FAILED.quota",
        "VERIFY_FAILED",
        "VERIFIED",
        "verify_result.json",
        "digest.md",
    ):
        (task_dir / stale).write_text("stale", encoding="utf-8")
    launch_script = SCRIPTS_DIR / "th-launch.sh"

    cmd = [
        str(launch_script),
        str(task_dir),
        "codex",
        "gpt-6.1-sol",
        "medium",
        str(task_dir),
    ]
    res = subprocess.run(cmd, env=test_env["env"], capture_output=True, text=True)
    assert res.returncode == 0, f"th-launch failed: {res.stderr}\n{res.stdout}"

    owner_file = task_dir / ".th_owner"
    assert owner_file.is_file(), ".th_owner must exist after th-launch"
    for stale in (
        "FAILED.quota",
        "VERIFY_FAILED",
        "VERIFIED",
        "verify_result.json",
        "digest.md",
    ):
        assert not (task_dir / stale).exists()
    assert owner_file.read_text(encoding="utf-8").strip() == str(test_env["workspace"])

    # Worker pid and registration in tasks.txt must also exist
    worker_pid_file = task_dir / "worker.pid"
    assert worker_pid_file.is_file(), "worker.pid must exist"
    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    assert str(task_dir) in tasks_txt.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "arguments", [[], ["codex"], ["codex", "worktree"], ["explicit"]]
)
def test_th_launch_passes_terra_model_and_effort_to_codex(
    test_env: dict, arguments: list[str]
):
    """PATH stub verifies explicit and omitted Codex Terra model/effort arguments."""
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
            *(
                ["codex", "gpt-5.6-terra", "medium", str(task_dir)]
                if arguments == ["explicit"]
                else [str(task_dir) if a == "worktree" else a for a in arguments]
            ),
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
        "gpt-5.6-terra",
        "-c",
        "model_reasoning_effort=medium",
    ]


def test_th_launch_marks_codex_as_worker_and_ends_option_parsing(test_env: dict):
    task_dir = _create_task_dir(test_env["tmp_path"], "task_role_codex")
    captured_args = test_env["tmp_path"] / "codex-args.txt"
    captured_env = test_env["tmp_path"] / "codex-env.txt"
    codex_stub = test_env["bin_dir"] / "codex"
    codex_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{captured_args}"\nprintf "%s" "$TH_ROLE" > "{captured_env}"\n',
        encoding="utf-8",
    )
    codex_stub.chmod(codex_stub.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        [str(SCRIPTS_DIR / "th-launch.sh"), str(task_dir)],
        env=test_env["env"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    deadline = time.time() + 3
    while not captured_args.exists() and time.time() < deadline:
        time.sleep(0.05)
    argv = captured_args.read_text(encoding="utf-8").splitlines()
    assert captured_env.read_text(encoding="utf-8") == "worker"
    assert argv[argv.index("project_doc_max_bytes=0") - 1] == "-c"
    separator = argv.index("--")
    assert argv[separator + 1].startswith("너는 team-harness 워커다")


@pytest.mark.parametrize("worker_type", ["codex", "claude"])
def test_th_launch_sets_image_permission_to_blocked_by_default(
    test_env: dict, worker_type: str
):
    task_dir = _create_task_dir(test_env["tmp_path"], f"task_image_{worker_type}")
    permission_dump = test_env["tmp_path"] / f"{worker_type}-image-permission.txt"
    stub = test_env["bin_dir"] / worker_type
    stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s" "$TH_ALLOW_IMAGES" > "{permission_dump}"\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        [
            str(SCRIPTS_DIR / "th-launch.sh"),
            str(task_dir),
            worker_type,
            "test-model",
            "medium",
            str(task_dir),
        ],
        env={
            **test_env["env"],
            **({"TH_USER_REQUESTED_CLAUDE": "1"} if worker_type == "claude" else {}),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    deadline = time.time() + 3
    while not permission_dump.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert permission_dump.read_text(encoding="utf-8") == "0"


def test_th_launch_rejects_claude_without_user_request(test_env: dict):
    """Claude workers require a user-request marker before any CLI runs."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_claude_rejected")

    result = subprocess.run(
        [str(SCRIPTS_DIR / "th-launch.sh"), str(task_dir), "claude"],
        env=test_env["env"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 4
    assert (
        result.stdout.strip()
        == "Claude 워커는 사용자 명령 시에만: TH_USER_REQUESTED_CLAUDE=1"
    )
    assert not (task_dir / "worker.pid").exists()


def test_th_launch_allows_user_requested_claude_with_sonnet_default(test_env: dict):
    """The marker permits the default Sonnet launch through the PATH stub only."""
    task_dir = _create_task_dir(test_env["tmp_path"], "task_claude_allowed")
    captured_args = test_env["tmp_path"] / "claude-args.txt"
    claude_stub = test_env["bin_dir"] / "claude"
    claude_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{captured_args}"\n',
        encoding="utf-8",
    )
    claude_stub.chmod(claude_stub.stat().st_mode | stat.S_IEXEC)

    result = subprocess.run(
        [str(SCRIPTS_DIR / "th-launch.sh"), str(task_dir), "claude"],
        env={**test_env["env"], "TH_USER_REQUESTED_CLAUDE": "1"},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    deadline = time.time() + 3
    while not captured_args.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert captured_args.read_text(encoding="utf-8").splitlines()[:4] == [
        "-p",
        "--dangerously-skip-permissions",
        "--model",
        "claude-sonnet-5",
    ]


def test_th_launch_uses_one_image_envelope_and_project_python_for_each_worker_type() -> (
    None
):
    source = (SCRIPTS_DIR / "th-launch.sh").read_text(encoding="utf-8")
    assert source.count('env "${worker_env[@]}" setsid nohup') == 3
    assert '"TH_WORKER_ALLOW_PATHS=$worker_allow_paths"' in source
    assert "TH_CODEX_PROJECT_DOC" in source
    assert source.count("project_doc_max_bytes=0") == 1
    # Codex and Claude receive a positional prompt. agy uses an unambiguous
    # --print=<value> option, so it has no separate positional prompt token.
    assert source.count('"$@" -- "$prompt"') == 2
    assert '"$@" --print="$prompt"' in source
    assert 'context_python=(uv run --directory "$script_dir/.." python)' in source
    assert (
        source.count(
            'PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" "${context_python[@]}"'
        )
        == 2
    )


def test_watch_scripts_import_team_harness_via_project_python() -> None:
    lib = (SCRIPTS_DIR / "th-watch-lib.sh").read_text(encoding="utf-8")
    status = (SCRIPTS_DIR / "th-status.sh").read_text(encoding="utf-8")
    assert (
        'uv run --directory "$lib_dir/.." python -m team_harness.watch_inspect "$task"'
        in lib
    )
    assert 'python3 - "$task"' not in lib
    assert 'uv run --directory "$script_dir/.." python - ' in status
    assert "python3 - " not in status
    assert "team_harness import failed" in status
    assert "context inspection failed" in lib


def test_install_journal_rule_replaces_existing_block_and_keeps_surroundings(
    tmp_path: Path,
):
    home = tmp_path / "home"
    claude = home / ".claude" / "CLAUDE.md"
    codex = home / ".codex" / "AGENTS.md"
    old = (
        "before\n<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:BEGIN -->\nold rule\n"
        "<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:END -->\nafter\n"
    )
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    claude.write_text(old, encoding="utf-8")
    codex.write_text(old, encoding="utf-8")

    result = subprocess.run(
        [str(SCRIPTS_DIR / "install-journal-rule.sh")],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    for target in (claude, codex):
        text = target.read_text(encoding="utf-8")
        assert text.startswith("before\n")
        assert text.endswith("\nafter\n")
        assert text.count("TEAM-HARNESS-COORDINATOR-JOURNAL:BEGIN") == 1
        assert "TH_ROLE=worker" in text
        assert "old rule" not in text


def test_journal_hook_is_noop_for_worker(tmp_path: Path):
    result = subprocess.run(
        [str(SCRIPTS_DIR / "journal-hook.sh")],
        cwd=tmp_path,
        env={**os.environ, "TH_ROLE": "worker"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == ""


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
        cmd = [str(launch_script), str(t), "codex", "gpt-6.1-sol", "medium", str(t)]
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


def test_th_watch_quota_error_keeps_task_for_later_completion(test_env: dict):
    """ops-05: A quota notice is non-terminal; a later DONE is still reported."""
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

    # Keep it registered: the CLI can recover after reporting quota exhaustion.
    assert str(task_dir) in tasks_txt.read_text(encoding="utf-8")

    (task_dir / "DONE").write_text("recovered\n", encoding="utf-8")
    res = subprocess.run(
        [str(watch_script), "2"], env=env, capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "완료" in res.stdout
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

        # Seed the actual idle signature with an elapsed threshold. A made-up
        # signature resets the timer and depends on a second poll fitting into
        # the short watcher deadline (which uses whole seconds).
        state_file = task_dir / ".th_sig"
        signature = subprocess.check_output(
            [
                "bash",
                "-c",
                'source "$1"; printf "%s:%s" "$(cpu_ticks "$2")" "$(latest_signature "$3")"',
                "bash",
                str(SCRIPTS_DIR / "th-watch-lib.sh"),
                str(proc.pid),
                str(task_dir),
            ],
            text=True,
        )
        state_file.write_text(f"{signature}|1000000000\n", encoding="utf-8")

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
        assert "실행 중" in res.stdout
        assert "중단" not in res.stdout
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
        [str(status_script), "-a"], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "중단?" in res.stdout


def test_th_status_default_hides_old_completed_tasks_but_all_shows_them(test_env: dict):
    """The compact default stays within 12 lines; -a retains the full table."""
    tasks = []
    old_time = time.time() - 48 * 60 * 60
    for index in range(12):
        task_dir = _create_task_dir(test_env["tmp_path"], f"old_done_{index}")
        (task_dir / "DONE").write_text("done\n", encoding="utf-8")
        for path in task_dir.iterdir():
            os.utime(path, (old_time, old_time))
        tasks.append(task_dir)

    tasks_txt = test_env["watch_dir"] / "tasks.txt"
    tasks_txt.write_text("\n".join(map(str, tasks)) + "\n", encoding="utf-8")
    status_script = SCRIPTS_DIR / "th-status.sh"

    compact = subprocess.run(
        [str(status_script)], env=test_env["env"], capture_output=True, text=True
    )
    assert compact.returncode == 0
    assert len(compact.stdout.splitlines()) <= 12
    assert "old_done_0" not in compact.stdout
    assert "다시 출력" in compact.stdout.splitlines()[-1]

    full = subprocess.run(
        [str(status_script), "-a"], env=test_env["env"], capture_output=True, text=True
    )
    assert full.returncode == 0
    assert len(full.stdout.splitlines()) > 12
    assert "old_done_0" in full.stdout


def test_quota_error_line_ignores_echoed_prompt_but_matches_cli_error(test_env: dict):
    """Prompt/suffix quota wording must not create a lock; CLI errors still do."""
    suffix_line = "Quota/permission/license error → `FAILED` immediately"
    test_env["suffix_file"].write_text(suffix_line + "\n", encoding="utf-8")
    log = test_env["tmp_path"] / "codex.log"
    log.write_text(
        suffix_line + "\n- stream error: rate limit exceeded\n> RESOURCE_EXHAUSTED\n",
        encoding="utf-8",
    )
    library = SCRIPTS_DIR / "th-watch-lib.sh"
    command = f'source "{library}"; quota_error_line "{log}"'
    res = subprocess.run(
        ["bash", "-c", command], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert res.stdout == ""

    log.write_text("stream error: rate limit exceeded\n", encoding="utf-8")
    res = subprocess.run(
        ["bash", "-c", command], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert "rate limit exceeded" in res.stdout


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
    res = subprocess.run(
        cmd,
        env={**test_env["env"], "TH_USER_REQUESTED_CLAUDE": "1"},
        capture_output=True,
        text=True,
    )
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
    assert "주의 필요" in res.stdout
    assert "쿼터오류" in res.stdout

    res_all = subprocess.run(
        [str(status_script), "-a"], env=test_env["env"], capture_output=True, text=True
    )
    assert res_all.returncode == 0
    assert "워커 오" in res_all.stdout


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
        "gpt-6.1-sol",
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
        "gpt-6.1-sol",
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
        [str(status_script), "-a"], env=test_env["env"], capture_output=True, text=True
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
        "gpt-6.1-sol",
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


def test_quota_error_line_ignores_code_and_diff_lines(test_env: dict):
    """Regression (10-16): source/diff lines containing quota regexes must not lock."""
    log = test_env["tmp_path"] / "codex.log"
    log.write_text(
        "\n".join(
            [
                "      if (lower ~ /(hit your usage limit|usage limit reached|rate limit exceeded|resource_exhausted|quota exceeded|at capacity|capacity exceeded)/) last = $0",
                "+      if (lower ~ /(hit your usage limit|usage limit reached|rate limit exceeded)/) last = $0",
                "-      if (lower ~ /(hit your usage limit|usage limit reached)/) last = $0",
                'if re.search(r"ERROR[: ]|RESOURCE_EXHAUSTED|quota exceeded|rate limit exceeded", line):',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    library = SCRIPTS_DIR / "th-watch-lib.sh"
    command = f'source "{library}"; quota_error_line "{log}"'
    res = subprocess.run(
        ["bash", "-c", command], env=test_env["env"], capture_output=True, text=True
    )
    assert res.returncode == 0
    assert res.stdout == ""

    # th-status must also ignore code/diff lines in codex.log
    task_dir = _create_task_dir(test_env["tmp_path"], "task_code_diff_no_quota")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "codex.log").write_text(
        log.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (task_dir / "context_exit_requested.json").write_text(
        '{"action":"rollover","reason":"context_cap_exceeded"}\n', encoding="utf-8"
    )
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")
    status_res = subprocess.run(
        [str(SCRIPTS_DIR / "th-status.sh"), "-a"],
        env=test_env["env"],
        capture_output=True,
        text=True,
    )
    assert status_res.returncode == 0
    assert "워커 오류" not in status_res.stdout

    # Real Codex and agy CLI error forms must still match independently.
    for quota_line in (
        "ERROR: You've hit your usage limit. Please try again later.",
        "error: Individual quota reached. Please retry later.",
        'AGY_ERROR: {"short_error":"RESOURCE_EXHAUSTED: quota exceeded"}',
        "stream error: unexpected status 429 Too Many Requests",
    ):
        log.write_text(quota_line + "\n", encoding="utf-8")
        res_match = subprocess.run(
            ["bash", "-c", command], env=test_env["env"], capture_output=True, text=True
        )
        assert res_match.returncode == 0
        assert quota_line in res_match.stdout


def test_th_launch_and_watch_context_rollover_resume_and_status(test_env: dict):
    """Stage 3a: th-launch dispatches, rejects active conflict, parks on exit, resumes short, and renders status."""
    import json

    from team_harness.worker_context import registry_path_for_workspace

    env = dict(test_env["env"])
    env["HOME"] = str(test_env["tmp_path"])
    env["TEAM_HARNESS_WORKER_CONTEXT_ENABLED"] = "1"
    env["TH_POLL_SEC"] = "1"

    # Stub codex that records argv and emits thread.started JSON
    args_file = test_env["tmp_path"] / "codex_args.txt"
    codex_stub = test_env["bin_dir"] / "codex"
    codex_stub.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$@" > "{args_file}"\n'
        'printf \'{"type":"thread.started","thread_id":"sid-stage3a-1"}\\n\'\n'
        'sleep "${STUB_SLEEP:-0.2}"\n',
        encoding="utf-8",
    )
    codex_stub.chmod(0o755)

    rollout = (
        test_env["tmp_path"]
        / ".codex"
        / "sessions"
        / "2026"
        / "10"
        / "rollout-sid-stage3a-1.jsonl"
    )
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "session_id": "sid-stage3a-1",
                "payload": {
                    "call_id": "call-1",
                    "info": {"last_token_usage": {"input_tokens": 58000}},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    task_dir = _create_task_dir(test_env["tmp_path"], "task_ctx_3a")
    (task_dir / "task.md").write_text(
        "---\ntask_key: exp/stage3a\nfollow_up: normal\n---\n# Stage 3a Task\nRun step 1.\n",
        encoding="utf-8",
    )
    (task_dir / "STATUS").write_text("2026-10-16T10:00:00Z STEP1\n", encoding="utf-8")
    (task_dir / "WORKLOG.md").write_text(
        "key: exp/stage3a\ngoal: step 1\nnext step: step 2\n", encoding="utf-8"
    )

    launch_script = SCRIPTS_DIR / "th-launch.sh"
    res1 = subprocess.run(
        [str(launch_script), str(task_dir), "codex", str(test_env["workspace"])],
        env={**env, "STUB_SLEEP": "5"},
        capture_output=True,
        text=True,
    )
    assert res1.returncode == 0, res1.stderr

    # While active worker is alive, launching same task_key must be rejected
    res_conflict = subprocess.run(
        [str(launch_script), str(task_dir), "codex", str(test_env["workspace"])],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res_conflict.returncode != 0
    assert "already has an active worker" in res_conflict.stderr

    # Check th-status shows active context tag on running row in default view and full line in -a
    status_active = subprocess.run(
        [str(SCRIPTS_DIR / "th-status.sh")],
        env={**env, "COLUMNS": "120"},
        capture_output=True,
        text=True,
    )
    assert status_active.returncode == 0
    assert "▶ 실행 중" in status_active.stdout
    assert "맥락 58k/60k" in status_active.stdout
    assert "unknown" not in status_active.stdout

    status_active_all = subprocess.run(
        [str(SCRIPTS_DIR / "th-status.sh"), "-a"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert status_active_all.returncode == 0
    assert "exp/stage3a |" in status_active_all.stdout
    assert "| active |" in status_active_all.stdout

    # Kill the sleep worker and mark DONE so th-watch reaps and parks it
    w_pid = int((task_dir / "worker.pid").read_text(encoding="utf-8").strip())
    try:
        os.killpg(os.getpgid(w_pid), signal.SIGTERM)
    except OSError:
        pass
    time.sleep(0.2)
    (task_dir / "DONE").write_text("Step 1 done\n", encoding="utf-8")

    watch_res = subprocess.run(
        [str(SCRIPTS_DIR / "th-watch.sh"), "2"], env=env, capture_output=True, text=True
    )
    assert watch_res.returncode == 0
    assert "완료" in watch_res.stdout

    reg_path = registry_path_for_workspace(test_env["workspace"])
    reg_data = json.loads(reg_path.read_text(encoding="utf-8"))
    sessions = reg_data["sessions"]
    assert len(sessions) == 1
    assert sessions[0]["state"] == "parked"
    assert sessions[0]["session_id"] == "sid-stage3a-1"
    assert sessions[0]["input_tokens"] == 58000
    assert sessions[0]["measurement_status"] == "measured"

    # Re-add task to tasks.txt and check th-status renders 58k(measured) | parked | resume
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")
    (task_dir / "task.md").write_text(
        "---\ntask_key: exp/stage3a\nfollow_up: short\n---\n# Stage 3a Task\nQuick fix.\n",
        encoding="utf-8",
    )
    status_parked = subprocess.run(
        [str(SCRIPTS_DIR / "th-status.sh"), "-a"],
        env=env,
        capture_output=True,
        text=True,
    )
    assert status_parked.returncode == 0
    assert "맥락 58k(measured) | parked | resume" in status_parked.stdout

    # Launching with follow_up: short resumes sid-stage3a-1
    res_resume = subprocess.run(
        [str(launch_script), str(task_dir), "codex", str(test_env["workspace"])],
        env={**env, "STUB_SLEEP": "0.1"},
        capture_output=True,
        text=True,
    )
    assert res_resume.returncode == 0, res_resume.stderr
    time.sleep(0.3)
    recorded_args = args_file.read_text(encoding="utf-8").splitlines()
    assert "resume" in recorded_args
    assert "sid-stage3a-1" in recorded_args


def test_th_watch_long_running_wait_detection_and_job_preservation(test_env: dict):
    """Stage 3a: detect consecutive sleep polls while job.pid is alive, ignore prompt/false-positives, terminate only worker after grace, and notify on job completion."""
    env = dict(test_env["env"])
    env["HOME"] = str(test_env["tmp_path"])
    env["TEAM_HARNESS_WORKER_CONTEXT_ENABLED"] = "1"
    env["TH_POLL_SEC"] = "1"

    task_dir = _create_task_dir(test_env["tmp_path"], "task_long_job")
    (task_dir / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (task_dir / "task.md").write_text(
        "---\ntask_key: exp/long-job\nfollow_up: normal\n---\n# Long Job\nDo not sleep in a loop.\n",
        encoding="utf-8",
    )
    (task_dir / "STATUS").write_text(
        "2026-10-16T10:00:00Z TRAINING\n", encoding="utf-8"
    )
    (test_env["watch_dir"] / "tasks.txt").write_text(f"{task_dir}\n", encoding="utf-8")

    # Spawn detached long job and separate worker in distinct process groups
    job_proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    worker_proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        # 1. False positive check: when job.pid == worker.pid, it is NOT a detached job
        (task_dir / "worker.pid").write_text(f"{worker_proc.pid}\n", encoding="utf-8")
        (task_dir / "job.pid").write_text(f"{worker_proc.pid}\n", encoding="utf-8")
        (task_dir / "codex.log").write_text(
            "Do not poll in a loop (sleep -> check -> sleep)\n"
            '{"type":"item.completed","item":{"id":"1","type":"command_execution","command":"sleep 5"}}\n'
            '{"type":"item.completed","item":{"id":"2","type":"command_execution","command":"sleep 5"}}\n',
            encoding="utf-8",
        )
        subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "1"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert not (task_dir / "context_exit_requested.json").exists()

        # 2. Distinct job.pid, but only prompt echo / prose mentioning sleep -> no false positive
        (task_dir / "job.pid").write_text(f"{job_proc.pid}\n", encoding="utf-8")
        (task_dir / "codex.log").write_text(
            "Prompt echo: sleep 10 && ps -ef\n"
            "- sleep 30\n"
            '{"type":"item.completed","item":{"id":"1","type":"command_execution","command":"sleep 5"}}\n',
            encoding="utf-8",
        )
        subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "1"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert not (task_dir / "context_exit_requested.json").exists()

        # 3. Second consecutive sleep+status poll without artifact change triggers warning + exit request
        with (task_dir / "codex.log").open("a", encoding="utf-8") as lf:
            lf.write(
                '{"type":"item.completed","item":{"id":"2","type":"command_execution","command":"sleep 5 && ps -p '
                + str(job_proc.pid)
                + '"}}\n'
            )
        watch_warn = subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "2"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert watch_warn.returncode == 0
        assert "긴 실행 대기" in watch_warn.stdout
        assert (task_dir / "context_exit_requested.json").is_file()

        # 4. After grace timeout (TH_EXIT_GRACE_SEC=0), harness writes minimal worklog, kills worker group, preserves job.pid
        watch_kill = subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "2"],
            env={**env, "TH_EXIT_GRACE_SEC": "0", "TH_KILL_GRACE_SEC": "1"},
            capture_output=True,
            text=True,
        )
        assert watch_kill.returncode == 0
        assert (task_dir / "WORKLOG.md").is_file()
        assert worker_proc.poll() is not None, "worker process must be terminated"
        assert job_proc.poll() is None, "detached job.pid must remain alive"

        # 5. When job.pid finishes, th-watch reports job completion without fabricating DONE
        job_proc.terminate()
        job_proc.wait(timeout=5)
        watch_job_done = subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "2"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert watch_job_done.returncode == 0
        assert "잡 완료" in watch_job_done.stdout
        assert not (task_dir / "DONE").exists()
        assert (task_dir / ".th_job_completed").is_file()

        # 6. Result analysis launch forces fresh even when follow_up is short
        args_file = test_env["tmp_path"] / "post_job_args.txt"
        codex_stub = test_env["bin_dir"] / "codex"
        codex_stub.write_text(
            f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{args_file}"\n',
            encoding="utf-8",
        )
        codex_stub.chmod(0o755)
        (task_dir / "task.md").write_text(
            "---\ntask_key: exp/long-job\nfollow_up: short\n---\n# Analyze Long Job\n",
            encoding="utf-8",
        )
        res_post = subprocess.run(
            [
                str(SCRIPTS_DIR / "th-launch.sh"),
                str(task_dir),
                "codex",
                str(test_env["workspace"]),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert res_post.returncode == 0, res_post.stderr
        time.sleep(0.2)
        post_args = args_file.read_text(encoding="utf-8").splitlines()
        assert "resume" not in post_args
    finally:
        for proc in (job_proc, worker_proc):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_th_launch_handoff_preserves_job_pid_and_embeds_experiment_and_minimal_worklog(
    test_env: dict,
):
    """Stage 3a: handoff preserves job.pid/WORKLOG/EXPERIMENT, generates minimal worklog on invalid doc, and defers kill when pgid is shared."""
    env = dict(test_env["env"])
    env["HOME"] = str(test_env["tmp_path"])
    env["TEAM_HARNESS_WORKER_CONTEXT_ENABLED"] = "1"
    env["TH_POLL_SEC"] = "1"

    args_file = test_env["tmp_path"] / "handoff_args.txt"
    codex_stub = test_env["bin_dir"] / "codex"
    codex_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "{args_file}"\n', encoding="utf-8"
    )
    codex_stub.chmod(0o755)

    task_dir = _create_task_dir(test_env["tmp_path"], "task_handoff_exp")
    exp_dir = test_env["tmp_path"] / "experiments" / "resnet-baseline"
    exp_dir.mkdir(parents=True, exist_ok=True)
    exp_file = exp_dir / "EXPERIMENT.md"
    exp_file.write_text(
        "# Experiment resnet-baseline\nbaseline: acc=0.81\n", encoding="utf-8"
    )

    (task_dir / "task.md").write_text(
        "---\ntask_key: experiment/resnet-baseline\nfollow_up: normal\n---\n# ResNet Handoff\nContinue work.\n",
        encoding="utf-8",
    )
    # Write oversized (>60 lines) WORKLOG.md to trigger minimal worklog snapshot while preserving original
    oversized_wl = "\n".join(
        [f"line {i}: experiments/resnet-baseline/EXPERIMENT.md" for i in range(65)]
    )
    (task_dir / "WORKLOG.md").write_text(oversized_wl + "\n", encoding="utf-8")
    (task_dir / "codex.log").write_text(
        "SECRET_PREVIOUS_SESSION_LOG_DO_NOT_INLINE\n", encoding="utf-8"
    )
    (task_dir / "context_exit_requested.json").write_text(
        '{"action":"rollover","reason":"context_cap_exceeded"}\n', encoding="utf-8"
    )

    job_proc = subprocess.Popen(["sleep", "30"])
    same_group_worker = subprocess.Popen(["sleep", "30"])
    try:
        (task_dir / "job.pid").write_text(f"{job_proc.pid}\n", encoding="utf-8")
        (task_dir / "worker.pid").write_text("999997\n", encoding="utf-8")

        res = subprocess.run(
            [
                str(SCRIPTS_DIR / "th-launch.sh"),
                str(task_dir),
                "codex",
                str(test_env["workspace"]),
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        # job.pid and original WORKLOG.md must be preserved; minimal worklog created
        assert (task_dir / "job.pid").read_text(encoding="utf-8").strip() == str(
            job_proc.pid
        )
        assert (
            (task_dir / "WORKLOG.md").read_text(encoding="utf-8").startswith("line 0:")
        )
        assert (task_dir / "WORKLOG.minimal.md").is_file()

        time.sleep(0.2)
        prompt_text = args_file.read_text(encoding="utf-8")
        assert "Handoff WORKLOG" in prompt_text
        assert "Handoff EXPERIMENT" in prompt_text
        assert "baseline: acc=0.81" in prompt_text
        assert "SECRET_PREVIOUS_SESSION_LOG_DO_NOT_INLINE" not in prompt_text

        # When worker and job share the same PGID, forced kill must be deferred
        (task_dir / "worker.pid").write_text(
            f"{same_group_worker.pid}\n", encoding="utf-8"
        )
        (task_dir / "context_exit_requested.json").write_text(
            '{"action":"rollover","reason":"long_running_wait","requested_at_epoch":1}\n',
            encoding="utf-8",
        )
        (test_env["watch_dir"] / "tasks.txt").write_text(
            f"{task_dir}\n", encoding="utf-8"
        )
        watch_defer = subprocess.run(
            [str(SCRIPTS_DIR / "th-watch.sh"), "2"],
            env={**env, "TH_EXIT_GRACE_SEC": "0"},
            capture_output=True,
            text=True,
        )
        assert watch_defer.returncode == 0
        assert "보류" in watch_defer.stdout
        assert same_group_worker.poll() is None
        assert job_proc.poll() is None
    finally:
        for proc in (same_group_worker, job_proc):
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)


def test_th_status_sectioned_table_and_formatting(test_env: dict):
    """Verify sectioned human-readable table: classification, time units, EAW alignment, narrow width, and NO_COLOR."""
    import unicodedata

    now_ts = time.time()
    status_script = SCRIPTS_DIR / "th-status.sh"

    # 1. Running task (robot_vision3_autolabel -> robot3, started 42m ago, updated <1m ago)
    t_run = _create_task_dir(
        test_env["tmp_path"] / "robot_vision3_autolabel" / "outputs" / "runs",
        "한글작업_실행",
    )
    (t_run / ".th_owner").write_text(str(test_env["workspace"]), encoding="utf-8")
    (t_run / "codex.log").write_text(
        "model: gpt-5.6-terra\nreasoning effort: medium\n", encoding="utf-8"
    )
    (t_run / "STATUS").write_text(
        "2026-10-19T10:20:00+09:00 HUMAN smoke 진행\n", encoding="utf-8"
    )

    # 2. Failed task (updated 38m ago)
    t_fail = _create_task_dir(
        test_env["tmp_path"] / "robot_vision3_autolabel" / "outputs" / "runs", "eval_h1"
    )
    (t_fail / "codex.log").write_text(
        "model: gpt-5.6-terra\nreasoning effort: medium\n", encoding="utf-8"
    )
    (t_fail / "FAILED").write_text("self-check mismatch\n", encoding="utf-8")
    (t_fail / "STATUS").write_text(
        "2026-10-19T10:30:00+09:00 FAILED: self-check mismatch\n", encoding="utf-8"
    )
    fail_ts = now_ts - 38 * 60
    for p in t_fail.iterdir():
        os.utime(p, (fail_ts, fail_ts))

    # 3. Done within 24h (updated 2h ago)
    t_done = _create_task_dir(
        test_env["tmp_path"] / "team-harness" / "_outputs", "worker_read_policy"
    )
    (t_done / "codex.log").write_text(
        "model: gpt-5.6-terra\nreasoning effort: high\n", encoding="utf-8"
    )
    (t_done / "DONE").write_text("1034 passed\n", encoding="utf-8")
    (t_done / "STATUS").write_text(
        "2026-10-19T09:00:00+09:00 DONE: 1034 passed\n", encoding="utf-8"
    )
    done_ts = now_ts - 2 * 3600 - 120
    for p in t_done.iterdir():
        os.utime(p, (done_ts, done_ts))

    # 4. Dead task from 3 days ago (no DONE/FAILED, no live process)
    t_old_dead = _create_task_dir(
        test_env["tmp_path"] / "robot_vision3_autolabel" / "outputs" / "runs",
        "old_dead_task",
    )
    (t_old_dead / "worker.pid").write_text("999996\n", encoding="utf-8")
    (t_old_dead / "STATUS").write_text(
        "2026-10-16T10:00:00+09:00 WAITING_FOR_S_DISTILL\n", encoding="utf-8"
    )
    old_ts = now_ts - 3 * 24 * 3600
    for p in t_old_dead.iterdir():
        os.utime(p, (old_ts, old_ts))

    # 5. Task with only DONE in STATUS (no DONE file, updated 50m ago)
    t_status_done = _create_task_dir(
        test_env["tmp_path"] / "team-harness" / "_outputs", "status_only_done"
    )
    (t_status_done / "STATUS").write_text("상태: DONE\n", encoding="utf-8")
    sd_ts = now_ts - 50 * 60
    for p in t_status_done.iterdir():
        os.utime(p, (sd_ts, sd_ts))

    # Quota lock with unlock time
    (test_env["watch_dir"] / "quota_lock.codex").write_text(
        "2026-10-19 10:06 | task | You've hit your usage limit ... try again at 1:50 PM\n",
        encoding="utf-8",
    )

    proc = subprocess.Popen(["sleep", "30"])
    try:
        (t_run / "worker.pid").write_text(f"{proc.pid}\n", encoding="utf-8")
        start_42m = now_ts - 42 * 60
        os.utime(t_run / "worker.pid", (start_42m, start_42m))
        os.utime(t_run / "STATUS", (now_ts - 10, now_ts - 10))

        tasks_txt = test_env["watch_dir"] / "tasks.txt"
        tasks_txt.write_text(
            "\n".join(map(str, [t_run, t_fail, t_done, t_old_dead, t_status_done]))
            + "\n",
            encoding="utf-8",
        )

        res = subprocess.run(
            [str(status_script)],
            env={**test_env["env"], "COLUMNS": "100", "NO_COLOR": "1"},
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr
        out = res.stdout
        assert "\033[" not in out

        # Header counts: 실행 1 · 주의 1 · 끝남 2 · 오래된 미정리 1 (숨김)
        assert "실행 1 · 주의 1 · 끝남 2 · 오래된 미정리 1 (숨김)" in out
        assert "▶ 실행 중" in out
        assert "⚠ 주의 필요" in out
        assert "✓ 최근 끝남 (24h)" in out
        assert "old_dead_task" not in out
        assert "status_only_done" in out

        # Time formatting check: 42분, 방금, 38분 전, 2시간 전, 50분 전
        assert "42분" in out
        assert "방금" in out
        assert "38분 전" in out
        assert "2시간 전" in out
        assert "50분 전" in out
        assert "codex 잠김(13:50 해제)" in out

        # East Asian Width alignment check across table rows
        def disp_w(s: str) -> int:
            return sum(
                2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s
            )

        header_line = next(
            line
            for line in out.splitlines()
            if "프로젝트" in line and "워커/모델" in line
        )
        stage_col_offset = disp_w(header_line.split("단계")[0])
        for row_marker in ("한글작업_실행", "eval_h1", "worker_read_policy"):
            row_line = next(line for line in out.splitlines() if row_marker in line)
            # Verify the stage column starts at the exact same display width column
            acc_w = 0
            cut_idx = 0
            for idx, ch in enumerate(row_line):
                if acc_w == stage_col_offset:
                    cut_idx = idx
                    break
                acc_w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            assert acc_w == stage_col_offset, f"Misaligned row: {row_line}"
            assert row_line[cut_idx - 2 : cut_idx] == "  "

        # Narrow terminal (<80 cols) drops 워커/모델 column
        res_narrow = subprocess.run(
            [str(status_script)],
            env={**test_env["env"], "COLUMNS": "72", "NO_COLOR": "1"},
            capture_output=True,
            text=True,
        )
        assert res_narrow.returncode == 0
        assert "워커/모델" not in res_narrow.stdout
        assert "codex terra-med" not in res_narrow.stdout
    finally:
        proc.terminate()
        proc.wait()
