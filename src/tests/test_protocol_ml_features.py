from pathlib import Path
import subprocess

import pytest

from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.mode_c import _first_unfinished_stage
from team_harness.protocol.mode_c import resume_mode_c
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import create_worktree


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    (path / "README.md").write_text("root\n")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class DummyRunner:
    async def run_agent(self, *args: object, **kwargs: object) -> object:
        raise NotImplementedError()


def test_protocol_13_first_unfinished_stage_on_check_failure() -> None:
    """PROTOCOL-13: _first_unfinished_stage must resume at IMPLEMENT when CHECK failed."""
    state = ProtocolState(
        task_id="TASK-CHECK-FAIL",
        mode="C",
        stage=Stage.CHECK.value,
        status=StageStatus.BLOCKED.value,
        stage_statuses={
            Stage.DEFINE.value: StageStatus.DONE.value,
            Stage.GIT_PREFLIGHT.value: StageStatus.DONE.value,
            Stage.WORKTREE_SETUP.value: StageStatus.DONE.value,
            Stage.DESIGN.value: StageStatus.DONE.value,
            Stage.IMPLEMENT.value: StageStatus.DONE.value,
            Stage.TEST.value: StageStatus.DONE.value,
            Stage.CHECK.value: StageStatus.BLOCKED.value,
        },
    )
    # CHECK failed -> must re-enter at IMPLEMENT to fix code, NOT REVIEW
    resume_stage = _first_unfinished_stage(state)
    assert resume_stage == Stage.IMPLEMENT.value


@pytest.mark.asyncio
async def test_protocol_02_resume_mode_c_without_worktree(tmp_path: Path) -> None:
    """PROTOCOL-02: resume_mode_c must reject runs where worktree was never created."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_mgr = ProtocolStateManager(run_dir)
    original_blocker = "Repository has uncommitted tracked changes"
    state = ProtocolState(
        task_id="TASK-PREFLIGHT-FAIL",
        mode="C",
        stage=Stage.GIT_PREFLIGHT.value,
        status=StageStatus.BLOCKED.value,
        blocker=original_blocker,
        stage_statuses={
            Stage.DEFINE.value: StageStatus.DONE.value,
            Stage.GIT_PREFLIGHT.value: StageStatus.BLOCKED.value,
        },
        worktrees={},  # No worktree created
    )
    state_mgr.save_state(state)

    runner = DummyRunner()
    with pytest.raises(ValueError, match="worktree was never created"):
        await resume_mode_c(
            run_dir=run_dir,
            agent_runner=runner,  # type: ignore
        )

    # State file must preserve original blocker
    loaded = state_mgr.load_state()
    assert loaded is not None
    assert loaded.blocker == original_blocker


def test_protocol_15_symlink_ignored_dirs_in_create_worktree(tmp_path: Path) -> None:
    """PROTOCOL-15: create_worktree symlinks configured ignored directories."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    # Create ignored directories in main repo (e.g. data, .venv)
    data_dir = repo / "data"
    data_dir.mkdir()
    (data_dir / "large_dataset.bin").write_bytes(b"data123")

    venv_dir = repo / ".venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("home = /usr/bin")

    wt_base = tmp_path / "worktrees"

    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-SYMLINK",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
        symlink_dirs=["data", ".venv"],
    )

    wt_path = Path(ref.path)
    assert wt_path.exists()

    wt_data = wt_path / "data"
    assert wt_data.is_symlink()
    assert wt_data.resolve() == data_dir.resolve()
    assert (wt_data / "large_dataset.bin").read_bytes() == b"data123"

    wt_venv = wt_path / ".venv"
    assert wt_venv.is_symlink()
    assert wt_venv.resolve() == venv_dir.resolve()


def test_protocol_15_config_symlink_ignored_dirs() -> None:
    """PROTOCOL-15: ProtocolConfig has symlink_ignored_dirs (default off)."""
    cfg_default = ProtocolConfig()
    assert cfg_default.symlink_ignored_dirs == ()

    cfg_explicit = load_protocol_config(symlink_ignored_dirs=["data", ".venv"])
    assert cfg_explicit.symlink_ignored_dirs == ("data", ".venv")
