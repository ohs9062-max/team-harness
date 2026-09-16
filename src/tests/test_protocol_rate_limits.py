"""TH-D18: the protocol runner explains stage failures and opens family circuits.

A protocol run fans several workers out over the same few agent families, so a
single hard provider 429 would otherwise be re-paid once per remaining stage.
These tests pin ``rate_limits._utc_now`` before the fixture's ``resetsAt``
instant; without that the trips asserted on here expire the moment they are
created and the tests would rot with the wall clock.
"""

# pyright: reportMissingParameterType=false

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from team_harness.agents import rate_limits
from team_harness.agents.template import AgentTemplate
from team_harness.config import Config
from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.mode_c import _run_stage
from team_harness.protocol.mode_c import TeamHarnessAgentRunner
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import StageStatus
from team_harness.protocol.state import ProtocolStateManager

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "claude_rate_limit.jsonl"

# Just before the claude fixture's resetsAt (1784811600), so a trip created
# from it is still inside its window.
_PINNED_NOW = datetime.fromtimestamp(1784811500, tz=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    monkeypatch.setattr(rate_limits, "_utc_now", lambda: _PINNED_NOW)


def _fake_worker(tmp_path: Path) -> Path:
    """A worker CLI stand-in that can replay a captured rate-limit stream."""
    worker = tmp_path / "fake-worker"
    worker.write_text(
        """#!/bin/sh
if [ "$EMIT_RATE_LIMIT" = "1" ]; then
    /bin/cat "$RATE_LIMIT_FIXTURE"
    exit 1
fi
if [ "$EMIT_STREAM" = "1" ]; then
    /bin/cat "$STREAM_FIXTURE"
    exit 0
fi
if [ "$EMIT_PLAIN_FAILURE" = "1" ]; then
    echo 'FAILED src/tests/test_thing.py::test_math - assert 1 == 2' >&2
    exit 2
fi
printf '%s\\n' '{"type":"result","subtype":"success","is_error":false}'
""",
        encoding="utf-8",
    )
    worker.chmod(0o755)
    return worker


def _config(tmp_path: Path, worker: Path, *, enabled: bool = True) -> Config:
    return Config(
        provider="openai_compat",
        model="coordinator-model",
        api_base="https://example.invalid/v1",
        api_key="test-key",
        cwd=str(tmp_path),
        rate_limit_circuit_breaker=enabled,
        agent_templates={
            "claude": AgentTemplate(command=(str(worker),), model_flag="--model"),
            "codex": AgentTemplate(command=(str(worker),), model_flag="--model"),
        },
        allowed_agents=["claude", "codex"],
    )


def _runner(tmp_path: Path, *, enabled: bool = True) -> TeamHarnessAgentRunner:
    worker = _fake_worker(tmp_path)
    return TeamHarnessAgentRunner(
        config=_config(tmp_path, worker, enabled=enabled),
        log_dir=tmp_path / "logs",
        verbose=False,
    )


async def _run(runner: TeamHarnessAgentRunner, tmp_path: Path):
    """Run one `claude` stage on the fake worker, whatever mode the env selects."""
    return await runner.run_agent(
        agent_type="claude",
        prompt="do the thing",
        cwd=str(tmp_path),
        timeout_sec=10,
        model="claude-sonnet-5",
    )


def _emit_rate_limit(monkeypatch) -> None:
    """Make the next fake-worker run replay a captured hard 429 and exit 1."""
    monkeypatch.setenv("EMIT_RATE_LIMIT", "1")
    monkeypatch.setenv("RATE_LIMIT_FIXTURE", str(CLAUDE_FIXTURE))


def _emit_plain_failure(monkeypatch) -> None:
    """Make the next fake-worker run fail the way a broken test suite does."""
    monkeypatch.setenv("EMIT_PLAIN_FAILURE", "1")


async def test_hard_rate_limit_is_classified_and_trips_the_family(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)

    _emit_rate_limit(monkeypatch)
    result = await _run(runner, tmp_path)

    assert result.success is False
    assert result.spawned is True
    assert result.exit_code == 1
    classification = result.failure_classification
    assert classification is not None
    assert classification["category"] == "rate_limit"
    assert classification["family"] == "claude"
    assert classification["resets_at"] == "2026-07-23T13:00:00+00:00"
    # The stage error the mode state machines hand to _block now names the
    # cause instead of only "exited with code 1".
    assert "rate_limit" in (result.error_message or "")
    assert runner.breaker.active_trip("claude") is not None


async def test_second_stage_on_a_tripped_family_is_refused_without_spawning(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    _emit_rate_limit(monkeypatch)
    await _run(runner, tmp_path)
    registered_after_first = len(runner.manager.list_all())
    monkeypatch.delenv("EMIT_RATE_LIMIT")

    refused = await _run(runner, tmp_path)

    assert refused.success is False
    assert refused.spawned is False
    assert refused.exit_code is None
    assert refused.stdout_path == ""
    # No second worker process, agent record, or log file was created.
    assert len(runner.manager.list_all()) == registered_after_first
    assert refused.failure_classification is not None
    assert refused.failure_classification["category"] == "rate_limit"
    assert "not launched" in (refused.error_message or "")


async def test_a_tripped_family_does_not_block_a_different_family(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    _emit_rate_limit(monkeypatch)
    await _run(runner, tmp_path)

    monkeypatch.delenv("EMIT_RATE_LIMIT")

    other = await runner.run_agent(
        agent_type="codex", prompt="do the thing", cwd=str(tmp_path), timeout_sec=10
    )

    assert other.spawned is True
    assert other.success is True
    assert other.failure_classification is None
    assert runner.breaker.active_trip("codex") is None


async def test_ordinary_work_failure_is_not_classified_and_never_trips(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)

    _emit_plain_failure(monkeypatch)
    result = await _run(runner, tmp_path)

    assert result.success is False
    assert result.spawned is True
    assert result.exit_code == 2
    # A failing test suite is the worker doing its job and reporting bad news,
    # not a provider problem: no classification, no family block.
    assert result.failure_classification is None
    assert runner.breaker.active_trip("claude") is None


async def test_disabled_circuit_breaker_still_runs_but_never_refuses(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path, enabled=False)

    _emit_rate_limit(monkeypatch)
    first = await _run(runner, tmp_path)
    monkeypatch.delenv("EMIT_RATE_LIMIT")
    second = await _run(runner, tmp_path)

    assert first.success is False
    assert runner.breaker.active_trip("claude") is None
    # With the knob off the harness keeps retrying, exactly as before TH-D18.
    assert second.spawned is True
    assert second.success is True


async def test_run_stage_records_the_classification_and_audits_no_model(
    tmp_path, monkeypatch
):
    """A refused stage is FAILED, explained, and has no effective_model."""

    runner = _runner(tmp_path)
    _emit_rate_limit(monkeypatch)
    await _run(runner, tmp_path)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state_mgr = ProtocolStateManager(run_dir)
    state = ProtocolState(task_id="T-1", mode="C")

    result = await _run_stage(
        state=state,
        state_mgr=state_mgr,
        agent_runner=runner,
        stage="IMPLEMENT",
        role="IMPLEMENT",
        spec=ProtocolAgentSpec(agent_type="claude", model="claude-sonnet-5"),
        prompt="implement it",
        cwd=str(tmp_path),
        timeout_sec=10,
    )

    assert result.spawned is False
    assert state.stage_statuses["IMPLEMENT"] == StageStatus.FAILED.value
    handoff = state.handoffs[-1]
    assert handoff["spawned"] is False
    assert handoff["failure_classification"]["category"] == "rate_limit"
    # Nothing ran, so the audit trail must not claim a model was used (TH-D6).
    assert handoff["effective_model"] is None
    assert state.effective_model is None
    # The requested model is still recorded — that part did happen.
    assert handoff["requested_model"] == "claude-sonnet-5"

    persisted = state_mgr.load_state()
    assert persisted is not None
    assert persisted.handoffs[-1]["failure_classification"]["family"] == "claude"


async def test_a_real_worker_stream_has_its_reported_usage_captured(
    tmp_path, monkeypatch
):
    """The runner records what the worker said it spent, end to end."""
    stream = tmp_path / "usage.jsonl"
    stream.write_text(
        '{"type":"system","subtype":"init","session_id":"s1"}\n'
        '{"type":"result","subtype":"success","is_error":false,'
        '"total_cost_usd":0.25,'
        '"usage":{"input_tokens":900,"output_tokens":100,'
        '"cache_read_input_tokens":400}}\n',
        encoding="utf-8",
    )
    runner = _runner(tmp_path)
    monkeypatch.setenv("EMIT_STREAM", "1")
    monkeypatch.setenv("STREAM_FIXTURE", str(stream))

    result = await _run(runner, tmp_path)

    assert result.success is True
    assert result.usage == {
        "input_tokens": 900,
        "output_tokens": 100,
        "cached_input_tokens": 400,
        "cost_usd": 0.25,
        "source_event_type": "result",
    }
    assert result.duration_sec > 0


async def test_a_plain_text_worker_records_no_usage(tmp_path, monkeypatch):
    """Antigravity-style prose output must not imply a zero-cost stage."""
    runner = _runner(tmp_path)

    result = await _run(runner, tmp_path)

    assert result.success is True
    assert result.usage is None


async def test_a_refused_stage_records_neither_usage_nor_duration(
    tmp_path, monkeypatch
):
    runner = _runner(tmp_path)
    _emit_rate_limit(monkeypatch)
    await _run(runner, tmp_path)
    monkeypatch.delenv("EMIT_RATE_LIMIT")

    refused = await _run(runner, tmp_path)

    assert refused.spawned is False
    assert refused.usage is None
    assert refused.duration_sec == 0.0
