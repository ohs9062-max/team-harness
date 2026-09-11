from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from team_harness.protocol.git import git_preflight
from team_harness.protocol.git import is_git_repo
from team_harness.protocol.git import list_worktrees
from team_harness.protocol.git import relay_evidence


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


def test_is_git_repo_true_and_false(tmp_path: Path) -> None:
    non_git_dir = tmp_path / "non_git"
    non_git_dir.mkdir()
    assert is_git_repo(non_git_dir) is False

    git_dir = tmp_path / "repo"
    git_dir.mkdir()
    _init_repo(git_dir)
    assert is_git_repo(git_dir) is True


def test_git_preflight_clean_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)

    pf = git_preflight(repo)
    assert pf.root == str(repo.resolve())
    assert pf.branch == "main"
    assert pf.head == head
    assert pf.dirty is False
    assert pf.status_lines == []
    assert len(pf.worktrees) >= 1
    assert pf.worktrees[0]["worktree"] == str(repo.resolve())


def test_git_preflight_non_git_raises(tmp_path: Path) -> None:
    non_git = tmp_path / "empty"
    non_git.mkdir()
    with pytest.raises(ValueError, match="Not a Git repository"):
        git_preflight(non_git)


def test_git_preflight_dirty_detection_untracked_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Add an untracked file
    new_file = repo / "scratch.txt"
    new_file.write_text("untracked work", encoding="utf-8")

    pf = git_preflight(repo)
    assert pf.dirty is True
    assert any("scratch.txt" in line for line in pf.status_lines)


def test_git_preflight_dirty_detection_modified_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    # Modify existing file
    readme = repo / "README.md"
    readme.write_text("# Modified Content\n", encoding="utf-8")

    pf = git_preflight(repo)
    assert pf.dirty is True
    assert any("README.md" in line for line in pf.status_lines)


def test_git_preflight_is_strictly_read_only(tmp_path: Path) -> None:
    """Ensure git_preflight does not mutate the repository."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    dirty_file = repo / "dirty.txt"
    dirty_file.write_text("should remain unchanged", encoding="utf-8")

    pf = git_preflight(repo)
    assert pf.dirty is True
    # File must still exist and be intact
    assert dirty_file.exists()
    assert dirty_file.read_text(encoding="utf-8") == "should remain unchanged"


def test_list_worktrees(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    wts = list_worktrees(repo)
    assert len(wts) >= 1
    assert "worktree" in wts[0]
    assert "HEAD" in wts[0]


def test_relay_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    head = _init_repo(repo)

    evidence = relay_evidence(repo)
    assert evidence.branch == "main"
    assert evidence.head == head
    assert isinstance(evidence.recent_log, list)
    assert len(evidence.recent_log) >= 1
