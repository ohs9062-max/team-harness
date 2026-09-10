from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from team_harness.protocol.worktree import changed_files
from team_harness.protocol.worktree import checkpoint_worktree
from team_harness.protocol.worktree import create_worktree
from team_harness.protocol.worktree import diff_worktree
from team_harness.protocol.worktree import remove_worktree
from team_harness.protocol.worktree import verify_checkpoint


def _init_repo(path: Path) -> str:
    """Initialize a git repo with an initial commit and return head hash."""
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Runner"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    readme = path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=path,
        check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return head


def test_worktree_created_in_managed_directory_outside_target_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    wt_base = tmp_path / "team_harness_worktrees"
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-42",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )

    wt_path = Path(ref.path)
    assert wt_path.exists()
    # Must NOT be inside the target repository
    assert not str(wt_path.resolve()).startswith(str(repo.resolve()))
    # Must be inside the team-harness managed directory
    assert str(wt_path.resolve()).startswith(str(wt_base.resolve()))
    assert ref.branch == "task/TASK-42/pipeline"
    assert ref.base_commit == base_commit


def test_worktree_branches_from_frozen_base_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    first_commit = _init_repo(repo)

    # Add a second commit on main
    file2 = repo / "extra.txt"
    file2.write_text("extra", encoding="utf-8")
    subprocess.run(
        ["git", "add", "extra.txt"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "commit", "-m", "second commit"],
        cwd=repo,
        check=True,
        capture_output=True,
    )

    wt_base = tmp_path / "wt_base"
    # Create worktree explicitly from the first_commit (frozen base)
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-FROZEN",
        label="codex",
        base_commit=first_commit,
        worktrees_base_dir=wt_base,
    )

    wt_path = Path(ref.path)
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=wt_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_head == first_commit
    # File from second commit must not be in the worktree
    assert not (wt_path / "extra.txt").exists()


def test_base_working_tree_not_modified_by_worktree_operations(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    wt_base = tmp_path / "wt_base"
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-ISOLATION",
        label="gemini",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )

    wt_path = Path(ref.path)
    # Write only to the worktree
    new_code = wt_path / "feature.py"
    new_code.write_text("print('hello')", encoding="utf-8")

    # Base repo working directory must remain pristine
    assert not (repo / "feature.py").exists()
    status_base = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert status_base == ""


def test_checkpoint_and_diff_and_changed_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    wt_base = tmp_path / "wt_base"
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-DIFF",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )

    wt_path = Path(ref.path)
    # Checkpoint without changes should fail
    with pytest.raises(RuntimeError, match="No changes available"):
        checkpoint_worktree(wt_path, "TASK-DIFF", "pipeline")

    # Make changes
    app_file = wt_path / "app.py"
    app_file.write_text("def run():\n    return 42\n", encoding="utf-8")

    ckpt_hash = checkpoint_worktree(wt_path, "TASK-DIFF", "pipeline")
    assert len(ckpt_hash) == 40

    # Diff
    diff = diff_worktree(base_commit, ckpt_hash, wt_path)
    assert "def run():" in diff
    assert "+    return 42" in diff

    # Changed files
    changed = changed_files(base_commit, ckpt_hash, wt_path)
    assert changed == ["app.py"]

    # Verify checkpoint
    verify_checkpoint(wt_path, ref.branch, ckpt_hash, base_commit)


def test_verify_checkpoint_detects_mismatch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    wt_base = tmp_path / "wt_base"
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-MISMATCH",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )

    wt_path = Path(ref.path)
    (wt_path / "code.py").write_text("code", encoding="utf-8")
    ckpt_hash = checkpoint_worktree(wt_path, "TASK-MISMATCH", "pipeline")

    # Mismatched branch
    with pytest.raises(RuntimeError, match="Worktree branch changed"):
        verify_checkpoint(wt_path, "wrong-branch", ckpt_hash, base_commit)

    # Mismatched commit
    with pytest.raises(RuntimeError, match="Worker HEAD changed"):
        verify_checkpoint(
            wt_path, ref.branch, "0000000000000000000000000000000000000000", base_commit
        )


def test_remove_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_repo(repo)

    wt_base = tmp_path / "wt_base"
    ref = create_worktree(
        repo_root=repo,
        task_id="TASK-REMOVE",
        label="pipeline",
        base_commit=base_commit,
        worktrees_base_dir=wt_base,
    )
    assert Path(ref.path).exists()

    remove_worktree(ref.path, repo_root=repo)
    assert not Path(ref.path).exists()
