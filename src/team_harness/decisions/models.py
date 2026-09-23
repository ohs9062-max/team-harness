from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any
from typing import Literal

DecisionKind = Literal[
    "tool_selection", "worker_failure", "review_escalation", "agent_selection"
]


@dataclass(frozen=True)
class DecisionRouterSettings:
    """Configuration for the optional Jev decision backend."""

    enabled: bool = False
    backend: str = "jev"
    api_url: str = "https://api.typesafe.ai/v1/systemone"
    model: str = "jev-latest"
    confidence_threshold: float = 0.85
    timeout_s: float = 3.0
    max_state_chars: int = 4000
    tool_routing: bool = True

    def __post_init__(self) -> None:
        if self.backend != "jev":
            raise ValueError("decision_router.backend must be 'jev'")
        if not 0.0 <= self.confidence_threshold <= 1.0:
            raise ValueError(
                "decision_router.confidence_threshold must be between 0 and 1"
            )
        if self.timeout_s <= 0:
            raise ValueError("decision_router.timeout_s must be greater than 0")
        if self.max_state_chars <= 0:
            raise ValueError("decision_router.max_state_chars must be greater than 0")


@dataclass(frozen=True)
class DecisionRequest:
    kind: DecisionKind
    state: Any
    options: dict[str, str]
    fallback: str
    instructions: str

    def __post_init__(self) -> None:
        if not self.options:
            raise ValueError("decision options must not be empty")
        if self.fallback not in self.options:
            raise ValueError("decision fallback must be one of the options")


@dataclass(frozen=True)
class DecisionResult:
    kind: DecisionKind
    choice: str
    confidence: float | None
    applied: bool
    fallback_reason: str | None = None
    backend: str = "jev"
    model: str = "jev-latest"
    input_chars: int = 0
    probabilities: dict[str, float] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    latency_ms: int | None = None
