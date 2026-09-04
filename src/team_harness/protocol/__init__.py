"""Harness Protocol layer — MODE A/B/C state machines on top of team-harness."""

from team_harness.protocol.checks import CheckResult
from team_harness.protocol.checks import CheckRunner
from team_harness.protocol.checks import evaluate_checks
from team_harness.protocol.git import git_preflight
from team_harness.protocol.git import GitPreflight
from team_harness.protocol.git import is_git_repo
from team_harness.protocol.git import list_worktrees
from team_harness.protocol.mode_c import AgentRunner
from team_harness.protocol.mode_c import run_mode_c
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.protocol.models import AGENT_TYPE_MAP
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import LogicalAgent
from team_harness.protocol.models import ProtocolMode
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import resolve_agent_type
from team_harness.protocol.models import ReviewVerdict
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.models import UserSelection
from team_harness.protocol.models import WorktreeInfo
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import checkpoint_worktree
from team_harness.protocol.worktree import compute_repo_id
from team_harness.protocol.worktree import create_worktree
from team_harness.protocol.worktree import DEFAULT_WORKTREES_DIR
from team_harness.protocol.worktree import diff_worktree
from team_harness.protocol.worktree import get_worktree_path
from team_harness.protocol.worktree import remove_worktree
from team_harness.protocol.worktree import verify_checkpoint
from team_harness.protocol.worktree import WorktreeRef

__all__ = [
    "AGENT_TYPE_MAP",
    "DEFAULT_WORKTREES_DIR",
    "AgentRunner",
    "CheckResult",
    "CheckRunner",
    "CheckStatus",
    "GitPreflight",
    "LogicalAgent",
    "ProtocolMode",
    "ProtocolState",
    "ProtocolStateManager",
    "ReviewVerdict",
    "Stage",
    "StageStatus",
    "TeamHarnessAgentRunner",
    "UserSelection",
    "WorktreeInfo",
    "WorktreeRef",
    "checkpoint_worktree",
    "compute_repo_id",
    "create_worktree",
    "diff_worktree",
    "evaluate_checks",
    "get_worktree_path",
    "git_preflight",
    "is_git_repo",
    "list_worktrees",
    "remove_worktree",
    "resolve_agent_type",
    "run_mode_c",
    "verify_checkpoint",
]
