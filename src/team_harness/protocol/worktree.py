"""Git worktree and local checkpoint operations for the Harness Protocol.

Creates task-scoped worktrees for MODE A/B/C.  Never pushes, force-pushes,
resets, stashes, or modifies the base working tree.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import subprocess

_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9._-]+$")
_VALID_LABELS = {"codex", "gemini", "pipeline", "worker_1", "worker_2", "integration"}

DEFAULT_WORKTREES_DIR = Path.home() / ".team-harness" / "worktrees"


@dataclass(frozen=True)
class WorktreeRef:
    label: str
    branch: str
    path: str
    base_commit: str


def _git(
    args: list[str], cwd: str | Path | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )


def _validate_task_id(task_id: str) -> None:
    if not _SAFE_TASK_ID.fullmatch(task_id):
        raise ValueError(f"Unsafe TASK-ID for Git ref/path: {task_id}")


def compute_repo_id(repo_root: str | Path) -> str:
    """Compute a unique, filesystem-safe identifier for a repository."""
    root = Path(repo_root).resolve()
    path_hash = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:8]
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", root.name)
    return f"{safe_name}_{path_hash}"


def get_worktree_path(
    repo_root: str | Path,
    task_id: str,
    label: str,
    worktrees_base_dir: str | Path | None = None,
) -> Path:
    """Get the target filesystem path for a task worktree."""
    _validate_task_id(task_id)
    if label not in _VALID_LABELS:
        raise ValueError(f"Unsupported worktree label: {label}")
    root = Path(repo_root).resolve()
    repo_id = compute_repo_id(root)
    base_dir = (
        Path(worktrees_base_dir).resolve()
        if worktrees_base_dir is not None
        else DEFAULT_WORKTREES_DIR
    )
    return (base_dir / repo_id / task_id / label).resolve()


def create_worktree(
    repo_root: str | Path,
    task_id: str,
    label: str,
    base_commit: str,
    *,
    worktrees_base_dir: str | Path | None = None,
) -> WorktreeRef:
    """Create a new task worktree branching from *base_commit*.

    ``label`` must be one of ``codex``, ``gemini``, or ``pipeline``.
    The worktree is stored in the team-harness managed area:
    ``~/.team-harness/worktrees/<repo-id>/<task-id>/<label>``.
    """
    _validate_task_id(task_id)
    if label not in _VALID_LABELS:
        raise ValueError(f"Unsupported worktree label: {label}")

    root = Path(repo_root).resolve()
    branch = f"task/{task_id}/{label}"
    wt_path = get_worktree_path(
        root, task_id, label, worktrees_base_dir=worktrees_base_dir
    )

    if wt_path.exists():
        raise FileExistsError(f"Worktree path already exists: {wt_path}")

    # Validate branch name and commit
    _git(["check-ref-format", "--branch", branch], cwd=root)
    _git(["cat-file", "-e", f"{base_commit}^{{commit}}"], cwd=root)

    wt_path.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-b", branch, str(wt_path), base_commit], cwd=root)

    return WorktreeRef(
        label=label, branch=branch, path=str(wt_path), base_commit=base_commit
    )


def remove_worktree(
    worktree_path: str | Path, repo_root: str | Path | None = None, force: bool = True
) -> None:
    """Safely remove a worktree and prune Git worktree references."""
    wt = Path(worktree_path).resolve()
    root = Path(repo_root).resolve() if repo_root else None
    cmd = ["worktree", "remove"]
    if force:
        cmd.append("--force")
    cmd.append(str(wt))
    try:
        _git(cmd, cwd=root or wt.parent)
    except (subprocess.CalledProcessError, OSError):
        # Fallback prune
        if root:
            _git(["worktree", "prune"], cwd=root)


def checkpoint_worktree(worktree_path: str | Path, task_id: str, label: str) -> str:
    """Create a local checkpoint commit in the given worktree.

    Returns the resulting commit hash.  Never pushes.
    """
    _validate_task_id(task_id)
    wt = Path(worktree_path).resolve()
    _git(["add", "-A"], cwd=wt)
    staged = _git(["diff", "--cached", "--name-only"], cwd=wt).stdout.strip()
    if not staged:
        raise RuntimeError(f"No changes available for {label} checkpoint")
    _git(["commit", "-m", f"checkpoint({label}): {task_id}"], cwd=wt)
    return _git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()


def diff_worktree(base_commit: str, checkpoint: str, worktree_path: str | Path) -> str:
    """Return the diff between *base_commit* and *checkpoint*."""
    return _git(
        ["diff", "--no-ext-diff", "--binary", f"{base_commit}...{checkpoint}"],
        cwd=Path(worktree_path).resolve(),
    ).stdout


def changed_files(
    base_commit: str, checkpoint: str, worktree_path: str | Path
) -> list[str]:
    """Return the list of files changed between *base_commit* and *checkpoint*."""
    output = _git(
        ["diff", "--name-only", f"{base_commit}...{checkpoint}"],
        cwd=Path(worktree_path).resolve(),
    ).stdout
    return [line for line in output.splitlines() if line]


def verify_checkpoint(
    worktree_path: str | Path, branch: str, checkpoint: str, base_commit: str
) -> None:
    """Verify that a worktree's state matches expectations.

    Raises ``RuntimeError`` on mismatch.
    """
    wt = Path(worktree_path).resolve()
    actual_branch = _git(["branch", "--show-current"], cwd=wt).stdout.strip()
    actual_head = _git(["rev-parse", "HEAD"], cwd=wt).stdout.strip()

    if actual_branch != branch:
        raise RuntimeError(
            f"Worktree branch changed: expected {branch}, got {actual_branch}"
        )
    if actual_head != checkpoint:
        raise RuntimeError(
            f"Worker HEAD changed: expected {checkpoint}, got {actual_head}"
        )

    try:
        merge_base = _git(
            ["merge-base", base_commit, checkpoint], cwd=wt
        ).stdout.strip()
    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Worker checkpoint no longer descends from the frozen base commit"
        ) from None

    if merge_base != base_commit:
        raise RuntimeError(
            "Worker checkpoint no longer descends from the frozen base commit"
        )
