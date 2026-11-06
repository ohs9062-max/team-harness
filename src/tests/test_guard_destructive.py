import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

HOOK = Path(__file__).resolve().parents[2] / "scripts/hooks/th-guard-destructive.py"


def run_hook(
    tmp_path: Path,
    command: str | None = None,
    extra: dict[str, str] | None = None,
    raw: str | None = None,
):
    env = os.environ.copy()
    for key in ("TH_GUARD", "TH_ROLE", "TH_JOB_TASK", "TH_GUARD_MODE"):
        env.pop(key, None)
    env["TH_WATCH_DIR"] = str(tmp_path)
    env.update(extra or {})
    result = subprocess.run(
        [sys.executable, str(HOOK)],
        input=raw
        if raw is not None
        else json.dumps({"tool_input": {"command": command}}),
        text=True,
        capture_output=True,
        env=env,
        check=True,
    )
    return json.loads(result.stdout) if result.stdout else None


@pytest.mark.parametrize(
    "command",
    [
        "kill 123",
        "pkill python",
        "killall python",
        "systemctl stop foo",
        "systemctl restart foo",
        "tmux kill-session -t a",
        "rm /tmp/outputs/x",
        "mv /tmp/_outputs/x /tmp/y",
        "rsync --delete /tmp/x /tmp/dataset/y",
        "find /tmp/다운로드/생성물/x -delete",
        "echo x > job.pid",
        "echo x >> /tmp/job.pid",
        "tee job.pid",
        "rm job.pid",
        "touch FAILED",
        "printf x > STOPPED",
        "echo x > STOPPED_BY_USER",
        "git reset --hard",
        "git clean -f",
        "git worktree remove x",
        "git branch -D foo",
        "echo hi; kill 3",
        "echo hi\nkill 3",
    ],
)
def test_detect(tmp_path: Path, command: str):
    result = run_hook(tmp_path, command)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert '"ask"' not in json.dumps(result)
    assert len((tmp_path / "guard.log").read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "command",
    [
        "kill -0 123",
        "cat outputs/x",
        "rm /tmp/x",
        "echo FAILED",
        "cat <<EOF\nkill 123\nEOF",
        "ls _outputs",
        "grep job.pid foo",
        "echo 'kill 123'",
        "printf 'git reset --hard'",
        "systemctl status foo",
        "tmux list-sessions",
        "git reset --soft",
        "git branch -d foo",
        "find /tmp/dataset/x -print",
        "rsync /tmp/a /tmp/outputs/x",
        "cat job.pid",
        "echo STOPPED",
        "echo ' > ' job.pid",
        "echo '>' job.pid",
        "echo ';' kill 123",
        "cat <<-EOF\n\tkill 123\n\tEOF",
        "touch /tmp/other",
        "echo 'rm /tmp/outputs/a'",
    ],
)
def test_pass(tmp_path: Path, command: str):
    assert run_hook(tmp_path, command) is None
    assert not (tmp_path / "guard.log").exists()


def test_exceptions_and_invalid_input(tmp_path: Path):
    for env in ({"TH_GUARD": "off"}, {"TH_ROLE": "worker"}, {"TH_JOB_TASK": ""}):
        assert run_hook(tmp_path, "kill 3", env) is None
    assert run_hook(tmp_path, raw="{") is None


def test_approved_command_is_logged(tmp_path: Path):
    command = 'TH_GUARD_APPROVED="사용자가 중단 승인" kill 3'
    assert run_hook(tmp_path, command) is None
    record = json.loads((tmp_path / "guard.log").read_text())
    assert record == {
        "pattern": "kill",
        "command": command,
        "approved": "사용자가 중단 승인",
    }


@pytest.mark.parametrize(
    "command",
    [
        "TH_GUARD_APPROVED= kill 3",
        'TH_GUARD_APPROVED="   " kill 3',
        "echo hi; TH_GUARD_APPROVED=x kill 3",
    ],
)
def test_invalid_approval_is_denied(tmp_path: Path, command: str):
    result = run_hook(tmp_path, command)
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert '"ask"' not in json.dumps(result)
    assert "approved" not in json.loads((tmp_path / "guard.log").read_text())
