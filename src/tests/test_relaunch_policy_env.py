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
