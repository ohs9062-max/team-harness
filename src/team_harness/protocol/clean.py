"""Clean up worktrees and branches from completed, blocked, or cancelled protocol runs."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import logging
from pathlib import Path
import shutil
import subprocess

from team_harness.protocol.models import Stage
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import _resolve_repo_root
from team_harness.protocol.worktree import remove_worktree

logger = logging.getLogger(__name__)

DEFAULT_RUNS_DIR = Path.home() / ".team-harness" / "runs"
CLEANABLE_STATUSES = {"DONE", "BLOCKED", "CANCEL", "CANCELLED", "CANCELED"}
PROTECTED_BRANCHES = {"master", "main", "trunk", "develop", "dev", "HEAD"}


@dataclass
class CleanTarget:
    run_dir: Path
    task_id: str
    status: str
    stage: str
    worktrees: list[Path] = field(default_factory=list)
    branches: list[str] = field(default_factory=list)
    repo_root: Path | None = None


@dataclass
class CleanResult:
    dry_run: bool
    targets: list[CleanTarget] = field(default_factory=list)
    removed_worktrees: list[str] = field(default_factory=list)
    removed_branches: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def clean_protocol_runs(
    *,
    runs_dir: str | Path | None = None,
    repo_root: str | Path | None = None,
    task_id: str | None = None,
    run_dir: str | Path | None = None,
    dry_run: bool = True,
) -> CleanResult:
    """Find and clean worktrees and branches from completed or blocked runs."""
    base_runs_dir = Path(runs_dir).resolve() if runs_dir else DEFAULT_RUNS_DIR
    target_repo_path = Path(repo_root).resolve() if repo_root else None

    # Discover candidate run directories
    candidate_dirs: list[Path] = []
    if run_dir:
        candidate_dirs.append(Path(run_dir).resolve())
    elif task_id:
        specific = base_runs_dir / f"protocol-{task_id}"
        if specific.is_dir():
            candidate_dirs.append(specific)
        else:
            # Fallback scan for directory matching task_id
            for p in base_runs_dir.glob("protocol-*"):
                if p.is_dir() and task_id in p.name:
                    candidate_dirs.append(p)
    elif base_runs_dir.exists():
        candidate_dirs.extend(
            sorted([p for p in base_runs_dir.glob("protocol-*") if p.is_dir()])
        )

    targets: list[CleanTarget] = []

    for r_dir in candidate_dirs:
        state_mgr = ProtocolStateManager(r_dir)
        state = state_mgr.load_state()
        if not state:
            continue

        raw_status = (state.status or "").strip().upper()
        raw_stage = (state.stage or "").strip().upper()
        if raw_status not in CLEANABLE_STATUSES and raw_stage != Stage.FINAL.value:
            continue

        # Resolve repo root
        repo_for_run = target_repo_path
        if repo_for_run is None and state.target_repo:
            candidate_repo = Path(state.target_repo).resolve()
            if candidate_repo.exists():
                repo_for_run = candidate_repo

        # Collect worktrees and branches
        worktrees_to_clean: list[Path] = []
        branches_to_clean: list[str] = []

        for _label, wt_info in (state.worktrees or {}).items():
            if not wt_info:
                continue
            path_str = wt_info.get("path")
            branch_str = wt_info.get("branch")

            if path_str:
                wt_p = Path(path_str).resolve()
                if wt_p.exists() and wt_p not in worktrees_to_clean:
                    worktrees_to_clean.append(wt_p)
                if repo_for_run is None and wt_p.exists():
                    resolved = _resolve_repo_root(wt_p)
                    if resolved:
                        repo_for_run = resolved

            if branch_str and branch_str not in branches_to_clean:
                if branch_str not in PROTECTED_BRANCHES:
                    branches_to_clean.append(branch_str)

        targets.append(
            CleanTarget(
                run_dir=r_dir,
                task_id=state.task_id,
                status=state.status,
                stage=state.stage,
                worktrees=worktrees_to_clean,
                branches=branches_to_clean,
                repo_root=repo_for_run,
            )
        )

    result = CleanResult(dry_run=dry_run, targets=targets)
    if dry_run:
        return result

    # Execute actual deletion
    for target in targets:
        repo = target.repo_root
        current_branch: str | None = None
        if repo and repo.exists():
            try:
                out = subprocess.run(
                    ["git", "branch", "--show-current"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout.strip()
                if out:
                    current_branch = out
            except Exception:
                pass

        # 1. Remove worktrees
        for wt_p in target.worktrees:
            try:
                remove_worktree(wt_p, repo_root=repo, force=True)
                if wt_p.exists():
                    shutil.rmtree(wt_p, ignore_errors=True)
                result.removed_worktrees.append(str(wt_p))
            except Exception as e:
                result.errors.append(f"Failed to remove worktree {wt_p}: {e}")

        # 2. Delete branches
        if repo and repo.exists():
            for branch in target.branches:
                if branch == current_branch or branch in PROTECTED_BRANCHES:
                    continue
                try:
                    res = subprocess.run(
                        ["git", "branch", "-D", branch],
                        cwd=repo,
                        capture_output=True,
                        text=True,
                    )
                    if res.returncode == 0:
                        result.removed_branches.append(branch)
                except Exception as e:
                    result.errors.append(f"Failed to delete branch {branch}: {e}")

            # Prune git worktrees
            try:
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=repo,
                    capture_output=True,
                    text=True,
                )
            except Exception:
                pass

    return result
