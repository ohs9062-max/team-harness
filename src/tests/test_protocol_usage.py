"""What a run spent — and, just as importantly, how much of that is known.

The two rules these tests pin: a worker that reports nothing yields None
rather than a zero-filled record, and a total always says how many of the
stages that ran are actually reflected in it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from team_harness.protocol.usage import extract_usage
from team_harness.protocol.usage import summarize_usage
from team_harness.protocol.usage import WorkerUsage

# A claude stream-json tail, the shape `claude -p --output-format stream-json`
# ends with. Counters live in `usage`, cost at the top level.
_CLAUDE_STREAM = (
    '{"type":"system","subtype":"init","session_id":"s1"}\n'
    '{"type":"assistant","message":{"content":[{"type":"text","text":"working"}]}}\n'
    '{"type":"result","subtype":"success","is_error":false,"duration_ms":4210,'
    '"total_cost_usd":0.0321,'
    '"usage":{"input_tokens":1200,"output_tokens":340,'
    '"cache_read_input_tokens":800,"cache_creation_input_tokens":64}}\n'
)

# The Gemini/antigravity stream-json shape, which nests a `stats` block.
_GEMINI_STREAM = (
    '{"type":"init","session_id":"g1","model":"gemini-2.5-pro"}\n'
    '{"type":"result","status":"success","stats":{"total_tokens":2048}}\n'
)


def test_claude_stream_usage_is_read_including_cache_and_cost():
    usage = extract_usage(_CLAUDE_STREAM)

    assert usage is not None
    assert usage.input_tokens == 1200
    assert usage.output_tokens == 340
    assert usage.cached_input_tokens == 800
    assert usage.cost_usd == pytest.approx(0.0321)
    assert usage.source_event_type == "result"
    # No total was reported, so it is derived from input+output rather than
    # left unknown.
    assert usage.total_tokens is None
    assert usage.effective_total_tokens() == 1540


def test_gemini_stream_reports_only_a_total():
    usage = extract_usage(_GEMINI_STREAM)

    assert usage is not None
    assert usage.total_tokens == 2048
    assert usage.effective_total_tokens() == 2048
    # Absent is absent, not zero.
    assert usage.input_tokens is None
    assert usage.output_tokens is None
    assert usage.cost_usd is None
    assert usage.to_dict() == {"total_tokens": 2048, "source_event_type": "result"}


def test_plain_text_and_unparseable_output_report_nothing():
    """Antigravity prints prose; that must not become a zero-cost claim."""
    assert extract_usage("Implemented the feature. All tests pass.\n") is None
    assert extract_usage("") is None
    assert extract_usage("not json\n{broken\n") is None
    # A JSON stream with no counters anywhere is still "nothing reported".
    assert extract_usage('{"type":"result","subtype":"success"}\n') is None


def test_the_last_reporting_event_wins():
    """These streams report cumulative totals; the terminal one is complete."""
    stream = (
        '{"type":"token_count","token_count":{"total_tokens":10}}\n'
        '{"type":"token_count","token_count":{"total_tokens":99}}\n'
    )

    usage = extract_usage(stream)

    assert usage is not None
    assert usage.total_tokens == 99


def test_flattened_top_level_counters_are_accepted():
    usage = extract_usage('{"type":"result","prompt_tokens":7,"completion_tokens":3}\n')

    assert usage is not None
    assert usage.input_tokens == 7
    assert usage.output_tokens == 3


def test_nonsense_counter_values_are_rejected_not_coerced():
    usage = extract_usage(
        '{"type":"result","usage":{"input_tokens":-5,"output_tokens":"lots",'
        '"total_tokens":true},"total_cost_usd":-1}\n'
    )

    # Every value was unusable, so the whole record is "nothing reported".
    assert usage is None


def _handoff(**overrides: object) -> dict[str, object]:
    base = {
        "stage": "IMPLEMENT",
        "agent_type": "codex",
        "effective_model": "gpt-5.6-terra",
        "success": True,
        "spawned": True,
        "duration_sec": 10.0,
        "usage": None,
    }
    base.update(overrides)
    return base


def test_summary_totals_duration_and_splits_it_by_agent_and_model():
    totals = summarize_usage(
        [
            _handoff(
                agent_type="claude", effective_model="claude-sonnet-5", duration_sec=5.0
            ),
            _handoff(duration_sec=12.5),
            _handoff(duration_sec=2.5),
        ]
    )

    assert totals.stages_run == 3
    assert totals.total_duration_sec == pytest.approx(20.0)
    assert totals.duration_by_agent == {"claude": 5.0, "codex": 15.0}
    assert totals.duration_by_model == {"claude-sonnet-5": 5.0, "gpt-5.6-terra": 15.0}


def test_summary_adds_up_reported_tokens_and_cost():
    totals = summarize_usage(
        [
            _handoff(
                usage={"input_tokens": 100, "output_tokens": 20, "cost_usd": 0.01}
            ),
            _handoff(
                usage={"input_tokens": 300, "output_tokens": 40, "cost_usd": 0.02}
            ),
        ]
    )

    assert totals.stages_reporting_usage == 2
    assert totals.input_tokens == 400
    assert totals.output_tokens == 60
    assert totals.total_tokens == 460  # derived per stage, then summed
    assert totals.cost_usd == pytest.approx(0.03)
    assert totals.usage_is_complete is True


def test_a_partial_total_is_marked_incomplete():
    """The number that matters most: how many stages the total covers."""
    totals = summarize_usage(
        [
            _handoff(usage={"total_tokens": 500}),
            _handoff(agent_type="antigravity", usage=None),
        ]
    )

    assert totals.stages_run == 2
    assert totals.stages_reporting_usage == 1
    assert totals.total_tokens == 500
    assert totals.usage_is_complete is False
    # Cost was never reported by anyone, so it stays unknown, not 0.0.
    assert totals.cost_usd is None


def test_no_reported_usage_at_all_leaves_every_figure_unknown():
    totals = summarize_usage([_handoff(), _handoff()])

    assert totals.stages_run == 2
    assert totals.stages_reporting_usage == 0
    assert totals.usage_is_complete is False
    assert totals.total_tokens is None
    assert totals.input_tokens is None
    assert totals.cost_usd is None


def test_refused_stages_count_separately_and_cost_nothing():
    """A stage the harness refused to launch (TH-D18) consumed nothing."""
    totals = summarize_usage(
        [
            _handoff(duration_sec=8.0, usage={"total_tokens": 100}),
            _handoff(
                stage="REVIEW",
                spawned=False,
                success=False,
                effective_model=None,
                duration_sec=0.0,
            ),
        ]
    )

    assert totals.stages_run == 1
    assert totals.stages_refused == 1
    assert totals.stages_failed == 0
    assert totals.total_duration_sec == pytest.approx(8.0)
    # The refused stage is absent from the per-agent split entirely.
    assert totals.duration_by_agent == {"codex": 8.0}
    assert totals.usage_is_complete is True


def test_failed_stages_are_counted_but_still_charged_for():
    totals = summarize_usage(
        [
            _handoff(success=False, duration_sec=600.0, usage={"total_tokens": 50}),
            _handoff(success=True, duration_sec=3.0),
        ]
    )

    assert totals.stages_run == 2
    assert totals.stages_failed == 1
    # A worker that burned its full timeout still spent that time.
    assert totals.total_duration_sec == pytest.approx(603.0)
    assert totals.total_tokens == 50


def test_summary_of_an_empty_run_is_all_zeros_and_unknowns():
    totals = summarize_usage([])

    assert totals.stages_run == 0
    assert totals.usage_is_complete is False
    assert totals.to_dict()["total_tokens"] is None


def test_totals_serialize_for_the_run_record():
    totals = summarize_usage([_handoff(usage={"total_tokens": 7, "cost_usd": 0.5})])
    payload = totals.to_dict()

    assert payload["stages_run"] == 1
    assert payload["usage_is_complete"] is True
    assert payload["total_tokens"] == 7
    assert payload["cost_usd"] == 0.5
    assert payload["duration_by_agent"] == {"codex": 10.0}


def test_worker_usage_empty_record_is_recognized():
    assert WorkerUsage().is_empty() is True
    assert WorkerUsage(total_tokens=0).is_empty() is False
    assert WorkerUsage(total_tokens=0).effective_total_tokens() == 0


def test_token_counts_survive_state_persistence_but_secrets_do_not():
    """The masking key pattern matches "token" inside "input_tokens".

    Without the numeric exemption every persisted token count became
    "[MASKED]" and the whole spend summary read as "nothing reported", while
    the figures were sitting in the log all along.
    """
    from team_harness.protocol.state import _mask_value

    masked = _mask_value(
        {
            "usage": {
                "input_tokens": 900,
                "output_tokens": 100,
                "cached_input_tokens": 400,
                "total_tokens": 1000,
                "cost_usd": 0.25,
            },
            "api_key": "sk-abcdefghijklmnop",
            "token": "ghp_abcdefghijklmnop",
            "authorization": "Bearer abcdefghijkl",
        }
    )

    assert masked["usage"] == {
        "input_tokens": 900,
        "output_tokens": 100,
        "cached_input_tokens": 400,
        "total_tokens": 1000,
        "cost_usd": 0.25,
    }
    # Credentials are still masked — only real numbers are exempt.
    assert masked["api_key"] == "[MASKED]"
    assert masked["token"] == "[MASKED]"
    assert masked["authorization"] == "[MASKED]"


def test_a_non_numeric_value_under_a_token_key_is_still_masked():
    from team_harness.protocol.state import _mask_value

    assert _mask_value({"token": "sk-abcdefghijklmnop"})["token"] == "[MASKED]"
    assert _mask_value({"token": {"nested": "x"}})["token"] == "[MASKED]"
    assert _mask_value({"token": ["x"]})["token"] == "[MASKED]"
    # A bool is not a count.
    assert _mask_value({"token": True})["token"] == "[MASKED]"


def test_usage_round_trips_through_a_saved_state(tmp_path: Path):
    """End to end: what the runner recorded is what `status` can read back."""
    from team_harness.protocol.models import ProtocolState
    from team_harness.protocol.state import ProtocolStateManager

    state_mgr = ProtocolStateManager(tmp_path)
    state_mgr.save_state(
        ProtocolState(
            task_id="T-1",
            mode="C",
            handoffs=[
                _handoff(
                    usage={"input_tokens": 900, "output_tokens": 100, "cost_usd": 0.25}
                ),
                _handoff(agent_type="antigravity", usage=None, duration_sec=5.0),
            ],
        )
    )

    reloaded = state_mgr.load_state()
    assert reloaded is not None
    totals = summarize_usage(reloaded.handoffs)

    assert totals.total_tokens == 1000
    assert totals.cost_usd == pytest.approx(0.25)
    assert totals.total_duration_sec == pytest.approx(15.0)
    assert totals.usage_is_complete is False
    assert totals.stages_reporting_usage == 1
