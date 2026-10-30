import os
from pathlib import Path
import subprocess
import sys


def test_relaunch_restores_high_override_and_not_retry(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "---\nkind: code\ntask_key: test\n---\n작업을 수행한다.\n"
    )
    watch = tmp_path / "watch"
    watch.mkdir()
    env = dict(
        os.environ,
        PYTHONPATH=str(root / "src"),
        TH_HIGH_REASON="approved high",
        TH_BRIEF_OK="approved brief",
        TH_IGNORE_BUDGET="1",
        TH_RETRY_REASON="do not restore",
        TH_WATCH_DIR=str(watch),
    )
    subprocess.run(
        [sys.executable, "-m", "team_harness.launch_env", "save", str(task)],
        env=env,
        check=True,
    )
    assert "TH_RETRY_REASON" not in (task / ".th_launch_env").read_text()
    for key in ("TH_HIGH_REASON", "TH_BRIEF_OK", "TH_IGNORE_BUDGET", "TH_RETRY_REASON"):
        env.pop(key, None)
    env.update(TH_ROLLOVER="1", TH_PREFLIGHT_ONLY="1", TEAM_HARNESS_JEV="0")
    command = [
        str(root / "scripts/th-launch.sh"),
        str(task),
        "codex",
        "gpt-6.1-terra",
        "high",
        str(root),
    ]
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stdout + result.stderr
    (task / ".th_launch_env").write_text("{}")
    result = subprocess.run(command, env=env, text=True, capture_output=True)
    assert result.returncode == 4
    assert "TH_HIGH_REASON" in result.stderr


def test_full_launch_preserves_overrides_for_two_rollovers(tmp_path: Path) -> None:
    import time

    root = Path(__file__).resolve().parents[2]
    task = tmp_path / "task"
    task.mkdir()
    (task / "task.md").write_text(
        "---\nkind: code\ntask_key: env-keep\n---\nComplete task.\n"
    )
    (task / "WORKLOG.md").write_text("progress\n")
    home, watch, workspace, bin_dir = (
        tmp_path / name for name in ("home", "watch", "workspace", "bin")
    )
    for directory in (home, watch, workspace, bin_dir):
        directory.mkdir()
    suffix = tmp_path / "suffix.md"
    suffix.write_text("Synthetic worker.\n")
    fake = bin_dir / "codex"
    fake.write_text('#!/bin/sh\necho started >> "$TH_WORKER_OUTPUT_DIR/attempts"\n')
    fake.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TH_", "CODEX_"))}
    env.update(
        HOME=str(home),
        PATH=f"{bin_dir}:{os.environ.get('PATH', '')}",
        PYTHONPATH=str(root / "src"),
        TH_WATCH_DIR=str(watch),
        TH_WORKSPACE=str(workspace),
        TH_WORKER_SUFFIX=str(suffix),
        TH_NO_DAEMON="1",
        TEAM_HARNESS_JEV="0",
        TH_HIGH_REASON="approved high",
        TH_BRIEF_OK="synthetic test",
    )
    command = [
        str(root / "scripts/th-launch.sh"),
        str(task),
        "codex",
        "gpt-6.1-terra",
        "high",
        str(root),
    ]
    for attempt in range(1, 4):
        if attempt > 1:
            env.pop("TH_HIGH_REASON", None)
            env.pop("TH_BRIEF_OK", None)
            env["TH_ROLLOVER"] = "1"
        result = subprocess.run(
            command, env=env, text=True, capture_output=True, timeout=60
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "approved high" in (task / ".th_launch_env").read_text()
        assert (task / ".th_session_agent").exists()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if (task / "attempts").exists() and len(
                (task / "attempts").read_text().splitlines()
            ) >= attempt:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(f"fake codex did not start for attempt {attempt}")
