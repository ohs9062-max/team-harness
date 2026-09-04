from __future__ import annotations

from pathlib import Path

import pytest

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


def test_protocol_modes() -> None:
    assert ProtocolMode.A.value == "A"
    assert ProtocolMode.B.value == "B"
    assert ProtocolMode.C.value == "C"


def test_stages_contain_required_lifecycle_stages() -> None:
    expected_stages = {
        "DEFINE",
        "GIT_PREFLIGHT",
        "WORKTREE_SETUP",
        "BASE_FREEZE",
        "DESIGN",
        "IMPLEMENT",
        "TEST",
        "CHECK",
        "REVIEW",
        "FIX",
        "FINAL",
        "INDEPENDENT_WORK",
        "WORKER_GATE",
        "CROSS_REVIEW",
        "RESPONSE",
        "COMPARE",
        "WAITING_USER",
        "CODEX_MERGE",
        "RELAY_TRIGGER",
        "CONTEXT_SAVE",
        "GIT_VERIFY",
        "CONTINUE",
    }
    stage_values = {s.value for s in Stage}
    assert expected_stages.issubset(stage_values)


def test_stage_status_values() -> None:
    assert StageStatus.PENDING.value == "PENDING"
    assert StageStatus.RUNNING.value == "RUNNING"
    assert StageStatus.DONE.value == "DONE"
    assert StageStatus.FAILED.value == "FAILED"
    assert StageStatus.BLOCKED.value == "BLOCKED"
    assert StageStatus.WAIVED.value == "WAIVED"


def test_review_verdict_values() -> None:
    assert ReviewVerdict.PASS.value == "PASS"
    assert ReviewVerdict.FIX_REQUIRED.value == "FIX_REQUIRED"
    assert ReviewVerdict.BLOCKED.value == "BLOCKED"
    assert ReviewVerdict.PENDING.value == "PENDING"


def test_check_status_values() -> None:
    assert CheckStatus.PASS.value == "PASS"
    assert CheckStatus.FAIL.value == "FAIL"
    assert CheckStatus.WAIVED.value == "WAIVED"
    assert CheckStatus.NOT_CONFIGURED.value == "NOT_CONFIGURED"


def test_user_selection_values() -> None:
    expected = {"SELECT_CODEX", "SELECT_GEMINI", "SELECT_HYBRID", "REWORK", "CANCEL"}
    actual = {s.value for s in UserSelection}
    assert actual == expected


def test_agent_mapping_gemini_to_antigravity() -> None:
    """Must map logical role 'gemini' to team-harness agent type 'antigravity'."""
    assert AGENT_TYPE_MAP["gemini"] == "antigravity"
    assert resolve_agent_type("gemini") == "antigravity"
    assert resolve_agent_type("GEMINI") == "antigravity"
    assert resolve_agent_type(LogicalAgent.GEMINI.value) == "antigravity"


def test_agent_mapping_claude_and_codex() -> None:
    assert AGENT_TYPE_MAP["claude"] == "claude"
    assert AGENT_TYPE_MAP["codex"] == "codex"
    assert resolve_agent_type("claude") == "claude"
    assert resolve_agent_type("codex") == "codex"


def test_resolve_agent_type_invalid() -> None:
    with pytest.raises(ValueError, match="Unknown logical agent"):
        resolve_agent_type("unknown_agent")


def test_protocol_state_fields_and_sync() -> None:
    state = ProtocolState(
        task_id="TASK-100",
        mode="C",
        stage=Stage.DESIGN.value,
        status=StageStatus.RUNNING.value,
        target_repo="/path/to/repo",
        base_branch="main",
        base_commit="abc1234",
        logical_agent="gemini",
        backend_agent="antigravity",
        checkpoint="def5678",
        test_status=CheckStatus.PASS.value,
        review_verdict=ReviewVerdict.PASS.value,
        review_cycle=1,
        user_selection=UserSelection.SELECT_CODEX.value,
        previous_agent="claude",
        next_agent="codex",
        run_id="run-42",
    )

    assert state.task_id == "TASK-100"
    assert state.mode == "C"
    assert state.logical_agent == "gemini"
    assert state.backend_agent == "antigravity"
    assert state.checkpoint == "def5678"
    assert state.team_harness_run_id == "run-42"
    assert state.run_id == "run-42"


def test_protocol_state_run_id_bidirectional_sync() -> None:
    state1 = ProtocolState(task_id="T1", mode="A", run_id="run-abc")
    assert state1.team_harness_run_id == "run-abc"

    state2 = ProtocolState(task_id="T2", mode="B", team_harness_run_id="run-xyz")
    assert state2.run_id == "run-xyz"


def test_protocol_state_serialization_roundtrip() -> None:
    state = ProtocolState(
        task_id="TASK-200",
        mode="A",
        stage=Stage.CROSS_REVIEW.value,
        status=StageStatus.DONE.value,
        target_repo="/tmp/repo",
        base_branch="develop",
        base_commit="commit-111",
        worktrees={
            "codex": {
                "label": "codex",
                "branch": "task/TASK-200/codex",
                "path": "/tmp/wt/codex",
                "base_commit": "commit-111",
            }
        },
        logical_agent="codex",
        backend_agent="codex",
        checkpoint="ckpt-222",
        checkpoints={"codex": "ckpt-222"},
        user_selection=UserSelection.SELECT_HYBRID.value,
        review_verdict=ReviewVerdict.PASS.value,
        run_id="run-999",
        user_request="Refactor module X",
    )

    as_dict = state.to_dict()
    assert as_dict["task_id"] == "TASK-200"
    assert as_dict["mode"] == "A"
    assert as_dict["stage"] == "CROSS_REVIEW"
    assert as_dict["checkpoint"] == "ckpt-222"
    assert as_dict["run_id"] == "run-999"
    assert as_dict["team_harness_run_id"] == "run-999"

    restored = ProtocolState.from_dict(as_dict)
    assert restored.task_id == state.task_id
    assert restored.mode == state.mode
    assert restored.stage == state.stage
    assert restored.worktrees == state.worktrees
    assert restored.user_selection == state.user_selection
    assert restored.checkpoint == state.checkpoint
    assert restored.team_harness_run_id == state.team_harness_run_id


def test_protocol_state_manager_save_and_load(tmp_path: Path) -> None:
    from team_harness.protocol.state import ProtocolStateManager

    run_dir = tmp_path / "run_01"
    mgr = ProtocolStateManager(run_dir)

    # Initial state should be None
    assert mgr.load_state() is None

    state = ProtocolState(
        task_id="TASK-SAVE-LOAD",
        mode="C",
        stage=Stage.IMPLEMENT.value,
        status=StageStatus.RUNNING.value,
        run_id="run-01",
        checkpoint="commit-abc",
    )
    saved_path = mgr.save_state(state)
    assert saved_path.exists()
    assert saved_path == run_dir / "protocol_state.json"

    loaded = mgr.load_state()
    assert loaded is not None
    assert loaded.task_id == "TASK-SAVE-LOAD"
    assert loaded.mode == "C"
    assert loaded.stage == Stage.IMPLEMENT.value
    assert loaded.checkpoint == "commit-abc"
    assert loaded.team_harness_run_id == "run-01"


def test_protocol_state_manager_events_and_outputs(tmp_path: Path) -> None:
    from team_harness.protocol.state import ProtocolStateManager

    run_dir = tmp_path / "run_02"
    mgr = ProtocolStateManager(run_dir)

    events_path = mgr.append_event("stage.start", stage="DESIGN", task_id="TASK-EVT")
    assert events_path.exists()
    events_text = events_path.read_text(encoding="utf-8")
    assert "stage.start" in events_text
    assert "TASK-EVT" in events_text

    output_path = mgr.write_output(
        "DESIGN", "claude", "# Architecture Document\nContent"
    )
    assert output_path.exists()
    assert output_path == run_dir / "protocol_outputs" / "DESIGN" / "claude.md"
    assert "Architecture Document" in output_path.read_text(encoding="utf-8")


def test_protocol_state_manager_sensitive_masking(tmp_path: Path) -> None:
    from team_harness.protocol.state import ProtocolStateManager

    run_dir = tmp_path / "run_03"
    mgr = ProtocolStateManager(run_dir)

    state = ProtocolState(
        task_id="TASK-SECRET",
        mode="C",
        user_request="Use api_key='sk-1234567890abcdef123456' to connect",
    )
    mgr.save_state(state)

    raw_json = (run_dir / "protocol_state.json").read_text(encoding="utf-8")
    assert "sk-1234567890abcdef123456" not in raw_json
    assert "[MASKED]" in raw_json
