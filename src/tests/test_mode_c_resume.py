"""Resuming a MODE C run instead of re-running the whole pipeline.

A pipeline that blocks at REVIEW has already paid for DESIGN and IMPLEMENT.
`resume_mode_c` re-enters over the worktree that already exists so those worker
turns are not spent again, and refuses to resume when the durable inputs it
would need are gone rather than quietly starting over.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

from team_harness.protocol.mode_c import RESUMABLE_MODE_C_STAGES
from team_harness.protocol.mode_c import resume_mode_c
from team_harness.protocol.mode_c import run_mode_c
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import ReviewVerdict
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.state import ProtocolStateManager

from .test_mode_c import _init_git_repo
from .test_mode_c import MockAgentRunner

_PASSING_CHECK = [[sys.executable, "-c", "print('check pass')"]]


async def _blocked_at_review(tmp_path: Path, run_name: str):
    """Drive a MODE C run that gets through IMPLEMENT then blocks in REVIEW."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    run_dir = tmp_path / run_name
    runner = MockAgentRunner()
    runner.review_success = False
    runner.review_error = "Reviewer crashed"

    state = await run_mode_c(
        task_id="TASK-RESUME",
        user_request="Implement answer() function",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        worktrees_base_dir=tmp_path / "worktrees",
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )
    assert state.status == StageStatus.BLOCKED.value
    assert state.stage == Stage.REVIEW.value
    return repo, run_dir, state, runner


async def test_resume_defaults_to_the_stage_that_blocked_and_skips_paid_work(
    tmp_path: Path,
):
    repo, run_dir, blocked, first_runner = await _blocked_at_review(tmp_path, "run_1")
    assert len(first_runner.calls) == 3  # design, implement, failed review

    second = MockAgentRunner()
    state = await resume_mode_c(
        run_dir=run_dir,
        agent_runner=second,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert state.stage == Stage.FINAL.value
    assert state.review_verdict == ReviewVerdict.PASS.value
    # Only REVIEW ran again — DESIGN and IMPLEMENT were not re-paid.
    assert [call["agent_type"] for call in second.calls] == ["antigravity"]
    assert "MODE C — REVIEW" in second.calls[0]["prompt"]
    # It reused the existing worktree rather than creating a second one.
    assert list(state.worktrees) == ["pipeline"]
    assert second.calls[0]["cwd"] == blocked.worktrees["pipeline"]["path"]


async def test_resume_feeds_the_recorded_design_back_into_the_review_prompt(
    tmp_path: Path,
):
    """The reviewer still sees the design, which lives only in the run dir."""
    _, run_dir, _, _ = await _blocked_at_review(tmp_path, "run_2")

    second = MockAgentRunner()
    await resume_mode_c(
        run_dir=run_dir,
        agent_runner=second,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert "Plan: create feature.py." in second.calls[0]["prompt"]


async def test_resume_at_implement_reruns_implement_but_not_design(tmp_path: Path):
    _, run_dir, _, _ = await _blocked_at_review(tmp_path, "run_3")

    second = MockAgentRunner()
    state = await resume_mode_c(
        run_dir=run_dir,
        agent_runner=second,
        from_stage=Stage.IMPLEMENT.value,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert [call["agent_type"] for call in second.calls] == ["codex", "antigravity"]
    # The re-run IMPLEMENT is given the design that was already recorded.
    assert "Plan: create feature.py." in second.calls[0]["prompt"]


async def test_resume_at_design_reruns_the_whole_pipeline_in_place(tmp_path: Path):
    _, run_dir, blocked, _ = await _blocked_at_review(tmp_path, "run_4")

    second = MockAgentRunner()
    state = await resume_mode_c(
        run_dir=run_dir,
        agent_runner=second,
        from_stage=Stage.DESIGN.value,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert [call["agent_type"] for call in second.calls] == [
        "claude",
        "codex",
        "antigravity",
    ]
    # Still the same worktree and the same frozen base commit (TH-D16).
    assert state.worktrees["pipeline"]["path"] == blocked.worktrees["pipeline"]["path"]
    assert state.base_commit == blocked.base_commit


async def test_resume_refuses_when_the_worktree_is_gone(tmp_path: Path):
    """Uncommitted worker output cannot be reconstructed (TH-D11), so a missing
    worktree blocks with that reason instead of being silently recreated."""
    import shutil

    _, run_dir, blocked, _ = await _blocked_at_review(tmp_path, "run_5")
    shutil.rmtree(blocked.worktrees["pipeline"]["path"])

    second = MockAgentRunner()
    state = await resume_mode_c(
        run_dir=run_dir, agent_runner=second, auto_discover_checks=False
    )

    assert state.status == StageStatus.BLOCKED.value
    assert "worktree is missing" in (state.blocker or "")
    assert second.calls == []


async def test_resume_refuses_an_unknown_or_non_resumable_stage(tmp_path: Path):
    _, run_dir, _, _ = await _blocked_at_review(tmp_path, "run_6")
    runner = MockAgentRunner()

    for stage in (Stage.FIX.value, Stage.CHECK.value, "NOT_A_STAGE"):
        with pytest.raises(ValueError, match="Cannot resume MODE C"):
            await resume_mode_c(run_dir=run_dir, agent_runner=runner, from_stage=stage)
    assert runner.calls == []
    assert set(RESUMABLE_MODE_C_STAGES) == {"DESIGN", "IMPLEMENT", "REVIEW"}


async def test_resume_rejects_a_missing_state_or_a_mode_a_run(tmp_path: Path):
    runner = MockAgentRunner()
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="No protocol state"):
        await resume_mode_c(run_dir=empty, agent_runner=runner)

    mode_a_dir = tmp_path / "mode_a"
    mode_a_dir.mkdir()
    state_mgr = ProtocolStateManager(mode_a_dir)
    from team_harness.protocol.models import ProtocolState

    state_mgr.save_state(ProtocolState(task_id="T-A", mode="A"))
    with pytest.raises(ValueError, match="is MODE A, not MODE C"):
        await resume_mode_c(run_dir=mode_a_dir, agent_runner=runner)


async def test_a_resumed_run_still_records_checks_and_a_checkpoint(tmp_path: Path):
    _, run_dir, _, _ = await _blocked_at_review(tmp_path, "run_7")

    state = await resume_mode_c(
        run_dir=run_dir,
        agent_runner=MockAgentRunner(),
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.test_status == CheckStatus.PASS.value
    assert state.checkpoint is not None and len(state.checkpoint) == 40
    # The blocker from the failed attempt is cleared, not left dangling.
    assert state.blocker is None
    persisted = ProtocolStateManager(run_dir).load_state()
    assert persisted is not None
    assert persisted.status == StageStatus.DONE.value
