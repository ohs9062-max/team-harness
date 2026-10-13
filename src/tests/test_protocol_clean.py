from pathlib import Path
import subprocess

from click.testing import CliRunner

from team_harness.cli import main
from team_harness.protocol.clean import clean_protocol_runs
from team_harness.protocol.models import ProtocolState
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


def test_clean_protocol_runs_dry_run_and_yes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    wt_base = tmp_path / "worktrees"
    wt_base.mkdir()

    # Setup TASK-1: DONE
    ref1 = create_worktree(
        repo_root=repo,
        task_id="TASK-1",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    run_dir1 = runs_dir / "protocol-TASK-1"
    run_dir1.mkdir()
    state1 = ProtocolState(
        task_id="TASK-1",
        mode="C",
        status=StageStatus.DONE.value,
        target_repo=str(repo),
        worktrees={"pipeline": {"path": ref1.path, "branch": ref1.branch}},
    )
    ProtocolStateManager(run_dir1).save_state(state1)

    # Setup TASK-2: RUNNING (should NOT be cleaned)
    ref2 = create_worktree(
        repo_root=repo,
        task_id="TASK-2",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    run_dir2 = runs_dir / "protocol-TASK-2"
    run_dir2.mkdir()
    state2 = ProtocolState(
        task_id="TASK-2",
        mode="C",
        status=StageStatus.RUNNING.value,
        target_repo=str(repo),
        worktrees={"pipeline": {"path": ref2.path, "branch": ref2.branch}},
    )
    ProtocolStateManager(run_dir2).save_state(state2)

    # Setup TASK-3: BLOCKED
    ref3 = create_worktree(
        repo_root=repo,
        task_id="TASK-3",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    run_dir3 = runs_dir / "protocol-TASK-3"
    run_dir3.mkdir()
    state3 = ProtocolState(
        task_id="TASK-3",
        mode="C",
        status=StageStatus.BLOCKED.value,
        target_repo=str(repo),
        worktrees={"pipeline": {"path": ref3.path, "branch": ref3.branch}},
    )
    ProtocolStateManager(run_dir3).save_state(state3)

    # Setup TASK-4: CANCELLED
    ref4 = create_worktree(
        repo_root=repo,
        task_id="TASK-4",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    run_dir4 = runs_dir / "protocol-TASK-4"
    run_dir4.mkdir()
    state4 = ProtocolState(
        task_id="TASK-4",
        mode="A",
        status="CANCELLED",
        target_repo=str(repo),
        worktrees={"pipeline": {"path": ref4.path, "branch": ref4.branch}},
    )
    ProtocolStateManager(run_dir4).save_state(state4)

    # 1. Dry run
    res_dry = clean_protocol_runs(runs_dir=runs_dir, dry_run=True)
    assert res_dry.dry_run is True
    assert len(res_dry.targets) == 3
    task_ids = {t.task_id for t in res_dry.targets}
    assert task_ids == {"TASK-1", "TASK-3", "TASK-4"}

    # In dry run, worktree paths and branches must still exist
    assert Path(ref1.path).exists()
    assert Path(ref2.path).exists()
    assert Path(ref3.path).exists()
    assert Path(ref4.path).exists()

    # 2. Actual clean (--yes)
    res_actual = clean_protocol_runs(runs_dir=runs_dir, dry_run=False)
    assert res_actual.dry_run is False
    assert len(res_actual.targets) == 3

    # TASK-1, TASK-3, TASK-4 worktrees must be removed
    assert not Path(ref1.path).exists()
    assert not Path(ref3.path).exists()
    assert not Path(ref4.path).exists()
    # TASK-2 (RUNNING) worktree must remain
    assert Path(ref2.path).exists()

    # Branches for TASK-1, TASK-3, TASK-4 must be deleted
    branches = subprocess.run(
        ["git", "branch"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    assert ref1.branch not in branches
    assert ref3.branch not in branches
    assert ref4.branch not in branches
    assert ref2.branch in branches


def test_cli_protocol_clean(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    wt_base = tmp_path / "worktrees"

    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-CLI-CLEAN",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    run_dir = runs_dir / "protocol-TASK-CLI-CLEAN"
    run_dir.mkdir()
    state = ProtocolState(
        task_id="TASK-CLI-CLEAN",
        mode="C",
        status=StageStatus.DONE.value,
        target_repo=str(repo),
        worktrees={"pipeline": {"path": ref.path, "branch": ref.branch}},
    )
    ProtocolStateManager(run_dir).save_state(state)

    runner = CliRunner()

    # Default invocation without --yes is dry-run
    res = runner.invoke(main, ["protocol", "clean", "--runs-dir", str(runs_dir)])
    assert res.exit_code == 0
    assert "dry-run" in res.output
    assert Path(ref.path).exists()

    # Invocation with --yes performs actual clean
    res_yes = runner.invoke(
        main, ["protocol", "clean", "--runs-dir", str(runs_dir), "--yes"]
    )
    assert res_yes.exit_code == 0
    assert not Path(ref.path).exists()
