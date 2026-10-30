import os
from pathlib import Path
import subprocess

import pytest

from team_harness.agents.spawner import spawn
from team_harness.config import Config


@pytest.mark.asyncio
async def test_codex_resume_uses_working_directory_without_cd_option(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, sys\n"
        "assert sys.argv[1:3] == ['exec', 'resume']\n"
        "if '-C' in sys.argv[3:] or '--cd' in sys.argv[3:]:\n"
        "    sys.exit(2)\n"
        "assert os.getcwd() == os.environ['EXPECTED_CWD']\n"
        "assert 'session-123' in sys.argv\n"
    )
    fake.chmod(0o755)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("EXPECTED_CWD", str(worktree))

    old = subprocess.run(
        [str(fake), "exec", "resume", "-C", str(worktree), "session-123", "go"],
        cwd=worktree,
        check=False,
    )
    assert old.returncode == 2
    result = await spawn(
        agent_id="resume",
        agent_type="codex",
        prompt="go",
        cwd=worktree,
        config=Config(),
        log_dir=tmp_path,
        mode="resume",
        resume_session_id="session-123",
    )
    assert await result.proc.wait() == 0
