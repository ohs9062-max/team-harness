"""What a protocol run actually spent: wall-clock time and reported tokens.

A MODE A run launches up to seven workers and a MODE C pipeline up to seven
stages, each a separate CLI subprocess on a paid model. The harness already
measured each stage's duration and threw it away, and never looked at the token
counts the worker CLIs report in their own output streams, so there was no way
to answer "what did that run cost?" short of reading provider dashboards.

Two rules shape this module, both about not overstating what is known:

* **Absent is not zero.** A worker that reports no usage yields ``None``, never
  a zero-filled record. Antigravity prints plain text and reports nothing at
  all, so a run including it can never have complete token figures.
* **A total says how complete it is.** `summarize_usage` counts the stages that
  reported usage alongside the stages that ran, so a token or cost total is
  never read as covering the whole run when it covers part of it.

Only wall-clock duration is measured by the harness itself and is therefore
always complete. Token counts and costs are the worker's own claims, parsed
from the stream it printed; the harness does not price models or convert
tokens into money.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import json
from typing import Any

# Token-count key aliases seen across the worker CLIs' terminal events. Each
# tuple is (canonical name, accepted keys in priority order).
_TOKEN_FIELDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("input_tokens", ("input_tokens", "inputTokens", "prompt_tokens")),
    ("output_tokens", ("output_tokens", "outputTokens", "completion_tokens")),
    (
        "cached_input_tokens",
        (
            "cache_read_input_tokens",
            "cacheReadInputTokens",
            "cached_input_tokens",
            "cached_tokens",
        ),
    ),
    ("total_tokens", ("total_tokens", "totalTokens", "total_token_count")),
)

_COST_KEYS: tuple[str, ...] = ("total_cost_usd", "totalCostUsd", "cost_usd")

# Containers a terminal event may nest its counters in. Claude Code uses
# `usage`, the Gemini/antigravity stream uses `stats`, and codex has shipped
# `token_count`/`token_usage` shapes; the counters are also accepted at the top
# level of the event for CLIs that flatten them.
_USAGE_CONTAINERS: tuple[str, ...] = ("usage", "stats", "token_count", "token_usage")


@dataclass(frozen=True)
class WorkerUsage:
    """Token counts and cost a single worker reported about its own run.

    Every field is optional because every CLI reports a different subset, and
    a missing count must stay missing rather than become a zero that a total
    would silently absorb.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    # Which event the numbers came from, so a reader can go back to the log.
    source_event_type: str | None = None

    def is_empty(self) -> bool:
        """True when the worker reported no usable number at all."""
        return all(
            getattr(self, name) is None
            for name in (
                "input_tokens",
                "output_tokens",
                "cached_input_tokens",
                "total_tokens",
                "cost_usd",
            )
        )

    def effective_total_tokens(self) -> int | None:
        """The worker's own total, else input+output when both are present."""
        if self.total_tokens is not None:
            return self.total_tokens
        if self.input_tokens is not None and self.output_tokens is not None:
            return self.input_tokens + self.output_tokens
        return None

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe form, omitting the fields the worker never reported."""
        return {
            key: value
            for key, value in {
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cached_input_tokens": self.cached_input_tokens,
                "total_tokens": self.total_tokens,
                "cost_usd": self.cost_usd,
                "source_event_type": self.source_event_type,
            }.items()
            if value is not None
        }


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if value >= 0 and value == value else None
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        # NaN compares unequal to itself; a negative cost is not a cost.
        return number if number == number and number >= 0 else None
    return None


def _usage_from_mapping(source: dict[str, object]) -> dict[str, Any]:
    """Pull recognized counters out of one candidate usage mapping."""
    found: dict[str, Any] = {}
    for canonical, aliases in _TOKEN_FIELDS:
        for alias in aliases:
            number = _as_int(source.get(alias))
            if number is not None:
                found[canonical] = number
                break
    for key in _COST_KEYS:
        cost = _as_float(source.get(key))
        if cost is not None:
            found["cost_usd"] = cost
            break
    return found


def _usage_from_event(event: dict[str, object]) -> dict[str, Any]:
    """Merge counters from an event's nested containers and its top level.

    Nested containers win over the top level, and earlier containers win over
    later ones, so a CLI that reports both a detailed `usage` block and a
    flattened summary is read from the detailed one.
    """
    merged: dict[str, Any] = {}
    for name in _USAGE_CONTAINERS:
        container = event.get(name)
        if isinstance(container, dict):
            for key, value in _usage_from_mapping(dict[str, object](container)).items():
                merged.setdefault(key, value)
    for key, value in _usage_from_mapping(event).items():
        merged.setdefault(key, value)
    return merged


def _json_events(stdout: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def extract_usage(stdout: str) -> WorkerUsage | None:
    """Read a worker's self-reported usage from its stdout stream.

    Returns None when the output is not an event stream (antigravity prints
    plain text) or carries no recognizable counter. The last event that
    reports anything wins, because these streams report cumulative totals and
    the terminal event is the complete one.
    """

    best: dict[str, Any] | None = None
    best_type: str | None = None
    for event in _json_events(stdout):
        found = _usage_from_event(event)
        if found:
            best = found
            event_type = event.get("type")
            best_type = event_type if isinstance(event_type, str) else None
    if not best:
        return None
    usage = WorkerUsage(source_event_type=best_type, **best)
    return None if usage.is_empty() else usage


@dataclass
class ProtocolUsageTotals:
    """Aggregate of what a run spent, with how much of it is actually known."""

    # Measured by the harness, so always complete for the stages that ran.
    total_duration_sec: float = 0.0
    stages_run: int = 0
    # Stages the harness refused to launch (TH-D18) — they cost nothing.
    stages_refused: int = 0
    stages_failed: int = 0

    # Reported by the workers, so complete only if every stage reported.
    stages_reporting_usage: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None

    # Per-agent and per-model duration, for seeing where the time went.
    duration_by_agent: dict[str, float] = field(default_factory=dict)
    duration_by_model: dict[str, float] = field(default_factory=dict)

    @property
    def usage_is_complete(self) -> bool:
        """True when every stage that actually ran reported its usage."""
        return self.stages_run > 0 and self.stages_reporting_usage == self.stages_run

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_duration_sec": round(self.total_duration_sec, 3),
            "stages_run": self.stages_run,
            "stages_refused": self.stages_refused,
            "stages_failed": self.stages_failed,
            "stages_reporting_usage": self.stages_reporting_usage,
            "usage_is_complete": self.usage_is_complete,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "total_tokens": self.total_tokens,
            "cost_usd": self.cost_usd,
            "duration_by_agent": {
                key: round(value, 3) for key, value in self.duration_by_agent.items()
            },
            "duration_by_model": {
                key: round(value, 3) for key, value in self.duration_by_model.items()
            },
        }


def _add(current: int | None, addition: int | None) -> int | None:
    if addition is None:
        return current
    return addition if current is None else current + addition


def _add_float(current: float | None, addition: float | None) -> float | None:
    if addition is None:
        return current
    return addition if current is None else current + addition


def summarize_usage(handoffs: list[dict[str, Any]]) -> ProtocolUsageTotals:
    """Roll up a run's recorded stage handoffs into one spend summary.

    Reads `state.handoffs`, which every mode appends to per stage, so this
    works the same for MODE A, B and C and for a state loaded back from disk.
    """

    totals = ProtocolUsageTotals()
    for handoff in handoffs:
        if handoff.get("spawned") is False:
            # Refused before launch, so it consumed no time and no tokens.
            totals.stages_refused += 1
            continue

        totals.stages_run += 1
        if not handoff.get("success"):
            totals.stages_failed += 1

        duration = _as_float(handoff.get("duration_sec")) or 0.0
        totals.total_duration_sec += duration
        agent = str(handoff.get("agent_type") or handoff.get("agent") or "unknown")
        totals.duration_by_agent[agent] = (
            totals.duration_by_agent.get(agent, 0.0) + duration
        )
        model = handoff.get("effective_model") or handoff.get("model")
        model_key = str(model) if model else "default"
        totals.duration_by_model[model_key] = (
            totals.duration_by_model.get(model_key, 0.0) + duration
        )

        raw_usage = handoff.get("usage")
        if not isinstance(raw_usage, dict):
            continue
        usage = WorkerUsage(
            input_tokens=_as_int(raw_usage.get("input_tokens")),
            output_tokens=_as_int(raw_usage.get("output_tokens")),
            cached_input_tokens=_as_int(raw_usage.get("cached_input_tokens")),
            total_tokens=_as_int(raw_usage.get("total_tokens")),
            cost_usd=_as_float(raw_usage.get("cost_usd")),
        )
        if usage.is_empty():
            continue
        totals.stages_reporting_usage += 1
        totals.input_tokens = _add(totals.input_tokens, usage.input_tokens)
        totals.output_tokens = _add(totals.output_tokens, usage.output_tokens)
        totals.cached_input_tokens = _add(
            totals.cached_input_tokens, usage.cached_input_tokens
        )
        totals.total_tokens = _add(totals.total_tokens, usage.effective_total_tokens())
        totals.cost_usd = _add_float(totals.cost_usd, usage.cost_usd)
    return totals
