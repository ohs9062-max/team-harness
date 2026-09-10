"""Read-only Git preflight and state inspection for the Harness Protocol."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess

# Exclude protocol runtime paths from dirty-state checks.
_RUNTIME_EXCLUDES = (":(exclude).harness", ":(exclude).harness/**")


@dataclass(frozen=True)
class GitPreflight:
    """Snapshot of a Git repository's state at preflight time."""

    root: str
    branch: str
    head: str
    dirty: bool
    status_lines: list[str]
    worktrees: list[dict[str, str]]


def _git(
    *args: str, cwd: str | Path, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        shell=False,
    )


def is_git_repo(path: str | Path) -> bool:
    """Return True if *path* is inside a Git repository."""
    try:
        _git("rev-parse", "--is-inside-work-tree", cwd=path)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def git_preflight(repo_path: str | Path) -> GitPreflight:
    """Perform a read-only Git preflight check.

    Raises ``ValueError`` if *repo_path* is not a Git repository.
    Does **not** modify the repository in any way (no stash, commit,
    reset, etc.).
    """
    repo = Path(repo_path).resolve()
    if not is_git_repo(repo):
        raise ValueError(f"Not a Git repository: {repo}")

    root = _git("rev-parse", "--show-toplevel", cwd=repo).stdout.strip()
    branch = _git("branch", "--show-current", cwd=repo).stdout.strip() or "DETACHED"
    head = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()

    status_output = _git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        ".",
        *_RUNTIME_EXCLUDES,
        cwd=repo,
    ).stdout
    status_lines = [line for line in status_output.splitlines() if line]

    worktrees = list_worktrees(repo)

    return GitPreflight(
        root=root,
        branch=branch,
        head=head,
        dirty=bool(status_lines),
        status_lines=status_lines,
        worktrees=worktrees,
    )


def list_worktrees(repo_path: str | Path) -> list[dict[str, str]]:
    """List registered Git worktrees in porcelain format."""
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    output = _git("worktree", "list", "--porcelain", cwd=repo_path).stdout
    for line in output.splitlines():
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def relay_evidence(worktree_path: str | Path) -> dict[str, object]:
    """Return the real Git facts a MODE B receiver must inspect.

    This is read-only — it never modifies the repository.
    """
    pf = git_preflight(worktree_path)
    recent_log = _git("log", "--oneline", "-5", cwd=worktree_path).stdout.splitlines()
    diff_stat = _git("diff", "--stat", cwd=worktree_path).stdout.splitlines()
    changed = _git(
        "diff", "--name-only", "HEAD", cwd=worktree_path, check=False
    ).stdout.splitlines()
    return {
        "branch": pf.branch,
        "head": pf.head,
        "status": pf.status_lines,
        "recent_log": recent_log,
        "diff_stat": diff_stat,
        "changed_files": [f for f in changed if f],
    }
