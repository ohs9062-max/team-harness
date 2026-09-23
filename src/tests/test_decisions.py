from __future__ import annotations

from dataclasses import dataclass

import pytest

from team_harness.decisions.jev_backend import _extract_answer
from team_harness.decisions.jev_backend import JevAnswer
from team_harness.decisions.jev_backend import JevBackendError
from team_harness.decisions.models import DecisionRequest
from team_harness.decisions.models import DecisionRouterSettings
from team_harness.decisions.router import DecisionRouter
from team_harness.decisions.tool_routing import build_tool_selection_request
from team_harness.decisions.tool_routing import schemas_for_profile


@dataclass
class FakeBackend:
    answer: JevAnswer | Exception

    async def decide(self, request):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def request() -> DecisionRequest:
    return DecisionRequest(
        kind="worker_failure",
        state={"failure": "rate_limit"},
        options={"RETRY": "retry", "BLOCK": "stop"},
        fallback="BLOCK",
        instructions="Choose the next action.",
    )


@pytest.mark.asyncio
async def test_router_applies_valid_high_confidence_choice():
    router = DecisionRouter(
        DecisionRouterSettings(enabled=True),
        backend=FakeBackend(
            JevAnswer(
                choice="RETRY",
                confidence=0.96,
                probabilities={"RETRY": 0.96, "BLOCK": 0.04},
                usage={"input_tokens": 12},
                latency_ms=25,
            )
        ),
    )
    result = await router.decide(request())
    assert result.choice == "RETRY"
    assert result.applied is True
    assert result.usage == {"input_tokens": 12}


@pytest.mark.asyncio
async def test_router_falls_back_on_low_confidence():
    router = DecisionRouter(
        DecisionRouterSettings(enabled=True, confidence_threshold=0.9),
        backend=FakeBackend(
            JevAnswer(
                choice="RETRY",
                confidence=0.6,
                probabilities={},
                usage={},
                latency_ms=10,
            )
        ),
    )
    result = await router.decide(request())
    assert result.choice == "BLOCK"
    assert result.applied is False
    assert "below threshold" in (result.fallback_reason or "")


@pytest.mark.asyncio
async def test_router_falls_back_on_backend_error():
    router = DecisionRouter(
        DecisionRouterSettings(enabled=True), backend=FakeBackend(RuntimeError("down"))
    )
    result = await router.decide(request())
    assert result.choice == "BLOCK"
    assert result.applied is False
    assert "down" in (result.fallback_reason or "")


def test_tool_state_is_bounded_and_profile_filters():
    selection = build_tool_selection_request(
        messages=[
            {"role": "system", "content": "s" * 100},
            {"role": "user", "content": "u" * 100},
        ],
        max_state_chars=40,
    )
    state_chars = sum(
        len(item["content"]) for item in selection.state["recent_messages"]
    )
    schemas = [
        {"type": "function", "function": {"name": "read_file"}},
        {"type": "function", "function": {"name": "write_file"}},
        {"type": "function", "function": {"name": "spawn_agent"}},
        {"type": "function", "function": {"name": "extension_tool"}},
    ]
    assert state_chars == 40
    assert schemas_for_profile(schemas=schemas, profile="MONITOR") == [
        schemas[0],
        schemas[3],
    ]
    assert schemas_for_profile(schemas=schemas, profile="FULL") == schemas


def test_jev_official_response_shape_is_parsed():
    answer = _extract_answer(
        {
            "model": "jev-latest",
            "answers": {
                "decision": {
                    "type": "choice",
                    "choice": "FULL",
                    "confidence": 0.9,
                    "probabilities": {"FULL": 0.9},
                }
            },
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
    )
    assert answer["choice"] == "FULL"


def test_jev_response_without_decision_is_rejected():
    with pytest.raises(JevBackendError):
        _extract_answer({"answers": {}})
