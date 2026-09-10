"""Data models for the Harness Protocol layer."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any


class ProtocolMode(str, Enum):
    """Execution modes defined by the Harness Protocol."""

    A = "A"  # PARALLEL COMPETITION
    B = "B"  # RELAY
    C = "C"  # ROLE PIPELINE


class Stage(str, Enum):
    """All possible stages across MODE A/B/C."""

    DEFINE = "DEFINE"
    GIT_PREFLIGHT = "GIT_PREFLIGHT"
    WORKTREE_SETUP = "WORKTREE_SETUP"
    BASE_FREEZE = "BASE_FREEZE"

    # MODE C stages
    DESIGN = "DESIGN"
    IMPLEMENT = "IMPLEMENT"
    TEST = "TEST"
    CHECK = "CHECK"
    REVIEW = "REVIEW"
    FIX = "FIX"
    FINAL = "FINAL"

    # MODE A stages
    INDEPENDENT_WORK = "INDEPENDENT_WORK"
    WORKER_GATE = "WORKER_GATE"
    CROSS_REVIEW = "CROSS_REVIEW"
    RESPONSE = "RESPONSE"
    COMPARE = "COMPARE"
    WAITING_USER = "WAITING_USER"
    USER_SELECT = "USER_SELECT"
    CODEX_MERGE = "CODEX_MERGE"

    # MODE B stages
    RELAY_TRIGGER = "RELAY_TRIGGER"
    CONTEXT_SAVE = "CONTEXT_SAVE"
    GIT_VERIFY = "GIT_VERIFY"
    CONTINUE = "CONTINUE"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    WAIVED = "WAIVED"


class ReviewVerdict(str, Enum):
    PASS = "PASS"
    FIX_REQUIRED = "FIX_REQUIRED"
    BLOCKED = "BLOCKED"
    PENDING = "PENDING"


class UserSelection(str, Enum):
    SELECT_WORKER_1 = "SELECT_WORKER_1"
    SELECT_WORKER_2 = "SELECT_WORKER_2"
    SELECT_HYBRID = "SELECT_HYBRID"
    REWORK = "REWORK"
    CANCEL = "CANCEL"

    # Legacy aliases
    SELECT_CODEX = "SELECT_CODEX"
    SELECT_GEMINI = "SELECT_GEMINI"


class CheckStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    WAIVED = "WAIVED"
    NOT_CONFIGURED = "NOT_CONFIGURED"


class LogicalAgent(str, Enum):
    """Logical agent roles in the Harness Protocol."""

    CLAUDE = "claude"
    CODEX = "codex"
    GEMINI = "gemini"


# Maps logical agent roles to team-harness agent type names.
# Key: logical role (as used in protocol documents)
# Value: team-harness agent type (as used in config.toml / spawner)
AGENT_TYPE_MAP: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "gemini": "antigravity",  # logical gemini → agent type antigravity → CLI agy
    "antigravity": "antigravity",
    "agy": "antigravity",
}


def normalize_agent_type(agent: str) -> str:
    """Normalize agent name or alias (e.g. gemini/agy -> antigravity).

    Preserves custom or unmapped agent names without premature rejection.
    """
    cleaned = agent.strip()
    if not cleaned:
        raise ValueError("agent_type must not be empty")
    lowered = cleaned.lower()
    return AGENT_TYPE_MAP.get(lowered, cleaned)


def resolve_agent_type(agent: str, valid_agents: set[str] | None = None) -> str:
    """Resolve an agent name to the team-harness agent type.

    Accepts logical roles ("gemini" -> "antigravity") or registered
    agent types ("antigravity", "claude", "codex", etc.).
    Raises ValueError for unknown agents.
    """
    lowered = agent.lower()
    if lowered in AGENT_TYPE_MAP:
        return AGENT_TYPE_MAP[lowered]
    if valid_agents is not None and lowered in valid_agents:
        return lowered
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES

    if lowered in DEFAULT_AGENT_TEMPLATES:
        return lowered
    known = sorted(
        set(list(AGENT_TYPE_MAP.keys()) + list(DEFAULT_AGENT_TEMPLATES.keys()))
    )
    raise ValueError(f"Unknown logical agent {agent!r}. Known agents: {known}")


@dataclass
class WorktreeInfo:
    """Reference to a task worktree."""

    label: str
    branch: str
    path: str
    base_commit: str


@dataclass
class AgentResult:
    """Result from a single agent stage execution."""

    agent: str  # logical agent name or spec agent
    agent_type: str  # team-harness agent type
    stage: str
    success: bool
    role: str = ""
    model: str | None = None
    requested_model: str | None = None
    effective_model: str | None = None
    exit_code: int | None = None
    stdout_path: str = ""
    stderr_path: str = ""
    output_text: str = ""
    changed_files: list[str] = field(default_factory=list)
    review_verdict: str | None = None
    duration_sec: float = 0.0
    error_message: str | None = None


@dataclass
class ProtocolState:
    """Runtime state for a Harness Protocol execution.

    Links to the team-harness run_id so protocol state and TH run.json
    coexist in the same run directory.
    """

    task_id: str
    mode: str  # "A", "B", "C"
    stage: str = Stage.DEFINE.value
    status: str = StageStatus.PENDING.value

    # Target repository
    target_repo: str = ""
    base_branch: str = ""
    base_commit: str = ""

    # Worktree info: label → WorktreeInfo dict
    worktrees: dict[str, dict[str, str]] = field(default_factory=dict)
    active_worktree: str | None = None
    active_branch: str | None = None

    # Agent tracking
    logical_agent: str | None = None
    backend_agent: str | None = None
    role: str | None = None
    model: str | None = None
    requested_model: str | None = None
    effective_model: str | None = None

    # Checkpoints: label → commit hash, and optional primary checkpoint
    checkpoints: dict[str, str] = field(default_factory=dict)
    checkpoint: str | None = None

    # Stage statuses: stage name → status
    stage_statuses: dict[str, str] = field(default_factory=dict)

    # Agent results: stage_key → list of result dicts
    agent_results: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # Handoff chain
    handoffs: list[dict[str, Any]] = field(default_factory=list)

    # CHECK results
    checks: list[dict[str, Any]] = field(default_factory=list)
    test_status: str | None = None

    # REVIEW
    review_verdict: str | None = None
    review_cycle: int = 0
    max_review_cycles: int = 2

    # MODE A specific
    worker_status: dict[str, str] = field(default_factory=dict)
    worker_branches: dict[str, str] = field(default_factory=dict)
    worker_tests: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    cross_reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    responses: dict[str, dict[str, Any]] = field(default_factory=dict)
    compare_path: str | None = None
    user_selection: str | None = None
    merge_status: str = "PENDING"

    # MODE B specific
    previous_agent: str | None = None
    next_agent: str | None = None
    relay: dict[str, Any] = field(default_factory=dict)
    git_discrepancies: list[str] = field(default_factory=list)

    # Changed files
    changed_files: list[str] = field(default_factory=list)

    # Link to team-harness run
    run_id: str = ""
    team_harness_run_id: str = ""

    # User request (original text)
    user_request: str = ""

    # Blocker reason
    blocker: str | None = None

    # Timestamps
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.team_harness_run_id and self.run_id:
            self.team_harness_run_id = self.run_id
        elif not self.run_id and self.team_harness_run_id:
            self.run_id = self.team_harness_run_id

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        result: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if isinstance(value, Enum):
                result[key] = value.value
            else:
                result[key] = value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProtocolState:
        """Deserialize from a dict (e.g. loaded from JSON)."""
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
