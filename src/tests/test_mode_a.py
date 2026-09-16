"""MODE A — PARALLEL COMPETITION, and the user selection that completes it.

MODE A is the largest protocol state machine and the one that spends the most:
two workers do the same task, review each other, respond, and only then does a
person choose. These tests pin the stage order, the fan-out, what each stage is
actually shown, and every way the run refuses to continue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.mode_a import resume_mode_a
from team_harness.protocol.mode_a import run_mode_a
from team_harness.protocol.models import AgentResult
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.state import ProtocolStateManager

from .test_mode_c import _init_git_repo

_PASSING_CHECK = [[sys.executable, "-c", "print('ok')"]]
_FAILING_CHECK = [[sys.executable, "-c", "raise SystemExit(1)"]]

_CHEAP_CONFIG = ProtocolConfig(
    mode_a_worker_1=ProtocolAgentSpec(
        agent_type="codex", model="gpt-5.6-terra", effort="high"
    ),
    mode_a_worker_2=ProtocolAgentSpec(
        agent_type="antigravity", model="Gemini 3.8 Flash (High)"
    ),
    mode_a_final=ProtocolAgentSpec(
        agent_type="codex", model="gpt-5.6-terra", effort="high"
    ),
)


class ModeARunner:
    """Programmable runner covering MODE A's four prompt kinds."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.concurrent_peak = 0
        self._in_flight = 0

        self.work_success = True
        self.work_error: str | None = None
        self.work_writes_file = True

        self.review_success = True
        self.review_output = "Looks reasonable overall.\nNo blocking issues."

        self.response_success = True
        self.response_output = "ACCEPT the naming point; REJECT the rewrite."

        self.merge_success = True
        self.merge_error: str | None = None
        self.merge_writes_file = True
        self.merge_spawned = True
        self.merge_failure_classification: dict[str, Any] | None = None

    async def run_agent(
        self,
        *,
        agent_type: str,
        prompt: str,
        cwd: str,
        timeout_sec: int,
        model: str | None = None,
        effort: str | None = None,
        label: str | None = None,
    ) -> AgentResult:
        self._in_flight += 1
        self.concurrent_peak = max(self.concurrent_peak, self._in_flight)
        try:
            # Yield twice so genuinely concurrent calls overlap here.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self.calls.append(
                {
                    "agent_type": agent_type,
                    "prompt": prompt,
                    "cwd": cwd,
                    "timeout_sec": timeout_sec,
                    "model": model,
                    "effort": effort,
                    "label": label,
                }
            )
            return self._result(agent_type, prompt, Path(cwd))
        finally:
            self._in_flight -= 1

    def _result(self, agent_type: str, prompt: str, cwd: Path) -> AgentResult:
        if "MODE A — INDEPENDENT_WORK" in prompt:
            if not self.work_success:
                return AgentResult(
                    agent=agent_type,
                    agent_type=agent_type,
                    stage=Stage.INDEPENDENT_WORK.value,
                    success=False,
                    error_message=self.work_error or "worker failed",
                )
            if self.work_writes_file:
                (cwd / "solution.py").write_text(
                    f"# by {cwd.name}\nvalue = 1\n", encoding="utf-8"
                )
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.INDEPENDENT_WORK.value,
                success=True,
                output_text=f"{cwd.name} implemented the request.",
            )

        if "MODE A — CROSS_REVIEW" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.CROSS_REVIEW.value,
                success=self.review_success,
                output_text=self.review_output if self.review_success else "",
                error_message=None if self.review_success else "reviewer failed",
            )

        if "MODE A — RESPONSE" in prompt:
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.RESPONSE.value,
                success=self.response_success,
                output_text=self.response_output if self.response_success else "",
                error_message=None if self.response_success else "responder failed",
            )

        if "MODE A — CODEX_MERGE" in prompt:
            if self.merge_success and self.merge_writes_file:
                (cwd / "integrated.py").write_text("merged = True\n", encoding="utf-8")
            return AgentResult(
                agent=agent_type,
                agent_type=agent_type,
                stage=Stage.CODEX_MERGE.value,
                success=self.merge_success,
                output_text="Integrated the selected work."
                if self.merge_success
                else "",
                error_message=self.merge_error,
                spawned=self.merge_spawned,
                failure_classification=self.merge_failure_classification,
            )

        raise AssertionError(f"Unrecognized MODE A prompt: {prompt[:120]}")


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, _init_git_repo(repo)


async def _run_to_waiting_user(
    tmp_path: Path,
    runner: ModeARunner,
    *,
    check_commands: list[list[str]] | None = None,
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo, _ = _repo(tmp_path)
    run_dir = tmp_path / "run"
    state = await run_mode_a(
        task_id="TASK-A",
        user_request="Implement value()",
        target_repo=str(repo),
        run_dir=run_dir,
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
        check_commands=check_commands or _PASSING_CHECK,
        auto_discover_checks=False,
    )
    return repo, run_dir, state


# ---------------------------------------------------------------------------
# run_mode_a
# ---------------------------------------------------------------------------


async def test_mode_a_reaches_waiting_user_with_a_compare_report(tmp_path: Path):
    runner = ModeARunner()
    _, run_dir, state = await _run_to_waiting_user(tmp_path, runner)

    assert state.status == "WAITING_USER"
    assert state.stage == Stage.WAITING_USER.value
    for stage in (
        Stage.DEFINE.value,
        Stage.GIT_PREFLIGHT.value,
        Stage.WORKTREE_SETUP.value,
        Stage.INDEPENDENT_WORK.value,
        Stage.WORKER_GATE.value,
        Stage.CROSS_REVIEW.value,
        Stage.RESPONSE.value,
        Stage.COMPARE.value,
    ):
        assert state.stage_statuses[stage] == StageStatus.DONE.value, stage

    assert set(state.worker_status) == {"worker_1", "worker_2"}
    assert all(v == StageStatus.DONE.value for v in state.worker_status.values())
    assert len(state.cross_reviews) == 2
    assert set(state.responses) == {"worker_1", "worker_2"}

    # The report a person actually reads exists and names both lanes.
    report = Path(state.compare_path or "").read_text(encoding="utf-8")
    assert "worker_1" in report and "worker_2" in report
    assert (run_dir / "protocol_outputs" / "COMPARE" / "report.md").is_file()


async def test_mode_a_isolates_each_worker_and_never_uses_the_base_repo(tmp_path: Path):
    """TH-D16: the base working tree is never a worker's cwd."""
    runner = ModeARunner()
    repo, _, state = await _run_to_waiting_user(tmp_path, runner)

    paths = {state.worktrees[w]["path"] for w in ("worker_1", "worker_2")}
    assert len(paths) == 2
    assert str(repo.resolve()) not in paths
    for call in runner.calls:
        assert call["cwd"] != str(repo.resolve())
    # Distinct branches, both cut from the one frozen base commit.
    assert state.worker_branches["worker_1"] != state.worker_branches["worker_2"]
    assert all(
        info["base_commit"] == state.base_commit for info in state.worktrees.values()
    )
    # The base repo is left clean.
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


async def test_mode_a_launches_each_stages_two_agents_concurrently(tmp_path: Path):
    """The 'PARALLEL' in PARALLEL COMPETITION (TH-D13)."""
    runner = ModeARunner()
    await _run_to_waiting_user(tmp_path, runner)

    assert runner.concurrent_peak == 2
    assert len(runner.calls) == 6  # 2 work + 2 review + 2 response


async def test_mode_a_cross_review_pairs_each_worker_against_the_other(tmp_path: Path):
    runner = ModeARunner()
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    assert set(state.cross_reviews) == {
        "worker_2_reviews_worker_1",
        "worker_1_reviews_worker_2",
    }
    for review in state.cross_reviews.values():
        assert review["reviewer"] != review["target"]
        assert review["success"] is True
    # A reviewer reads the *target's* worktree, not its own.
    review_calls = [c for c in runner.calls if "CROSS_REVIEW" in c["prompt"]]
    reviewed_paths = {c["cwd"] for c in review_calls}
    assert reviewed_paths == {
        state.worktrees["worker_1"]["path"],
        state.worktrees["worker_2"]["path"],
    }


async def test_mode_a_feeds_each_worker_the_review_of_its_own_work(tmp_path: Path):
    runner = ModeARunner()
    runner.review_output = "REVIEW BODY MARKER\nConsider renaming value()."
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    response_calls = [c for c in runner.calls if "MODE A — RESPONSE" in c["prompt"]]
    assert len(response_calls) == 2
    for call in response_calls:
        assert "REVIEW BODY MARKER" in call["prompt"]
    assert state.responses["worker_1"]["dispositions"] == ["ACCEPT", "REJECT"]


async def test_mode_a_passes_each_workers_configured_model_and_effort(tmp_path: Path):
    """TH-D17: the protocol's cheap-tier choice must reach the worker."""
    runner = ModeARunner()
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    by_agent = {c["agent_type"]: c for c in runner.calls}
    assert by_agent["codex"]["model"] == "gpt-5.6-terra"
    assert by_agent["codex"]["effort"] == "high"
    assert by_agent["antigravity"]["model"] == "Gemini 3.8 Flash (High)"
    # antigravity has no reasoning-effort flag, so none is sent.
    assert by_agent["antigravity"]["effort"] is None
    work_handoffs = [
        h for h in state.handoffs if h["stage"] == Stage.INDEPENDENT_WORK.value
    ]
    assert {h["effective_model"] for h in work_handoffs} == {
        "gpt-5.6-terra",
        "Gemini 3.8 Flash (High)",
    }


async def test_mode_a_blocks_at_the_worker_gate_when_a_worker_fails(tmp_path: Path):
    runner = ModeARunner()
    runner.work_success = False
    runner.work_error = "codex exited with code 1"
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.WORKER_GATE.value] == StageStatus.BLOCKED.value
    assert state.worker_status["worker_1"] == StageStatus.FAILED.value
    # The blocker names the lane and keeps the worker's own error, which used
    # to be overwritten by the generic message.
    assert "worker_1" in (state.blocker or "")
    assert "codex exited with code 1" in (state.blocker or "")
    # Both workers were launched (they run in parallel) but nothing after.
    assert len(runner.calls) == 2


async def test_mode_a_blocks_at_the_worker_gate_when_a_worker_check_fails(
    tmp_path: Path,
):
    runner = ModeARunner()
    _, _, state = await _run_to_waiting_user(
        tmp_path, runner, check_commands=_FAILING_CHECK
    )

    assert state.status == StageStatus.BLOCKED.value
    assert "check failed" in (state.blocker or "")
    assert state.worker_tests["worker_1"]
    assert any(not c["success"] for c in state.worker_tests["worker_1"])


async def test_mode_a_blocks_when_a_cross_review_fails(tmp_path: Path):
    runner = ModeARunner()
    runner.review_success = False
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.CROSS_REVIEW.value] == StageStatus.BLOCKED.value
    assert "Cross review failed" in (state.blocker or "")


async def test_mode_a_blocks_when_a_response_states_no_disposition(tmp_path: Path):
    """A clean review still needs an explicit disposition, not silence."""
    runner = ModeARunner()
    runner.response_output = "Nothing to add, the review found no issues."
    _, _, state = await _run_to_waiting_user(tmp_path, runner)

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.RESPONSE.value] == StageStatus.BLOCKED.value
    assert "Invalid MODE A response" in (state.blocker or "")
    assert state.responses["worker_1"]["dispositions"] == []


async def test_mode_a_blocks_on_a_dirty_base_repository(tmp_path: Path):
    repo, _ = _repo(tmp_path)
    (repo / "uncommitted.txt").write_text("dirty\n", encoding="utf-8")
    runner = ModeARunner()

    state = await run_mode_a(
        task_id="TASK-A",
        user_request="Implement value()",
        target_repo=str(repo),
        run_dir=tmp_path / "run",
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.GIT_PREFLIGHT.value] == StageStatus.BLOCKED.value
    assert "uncommitted changes" in (state.blocker or "")
    assert runner.calls == []


async def test_mode_a_blocks_on_a_non_git_target(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    runner = ModeARunner()

    state = await run_mode_a(
        task_id="TASK-A",
        user_request="Implement value()",
        target_repo=str(plain),
        run_dir=tmp_path / "run",
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert runner.calls == []


# ---------------------------------------------------------------------------
# resume_mode_a
# ---------------------------------------------------------------------------


async def test_select_worker_integrates_on_its_own_branch_and_leaves_base_clean(
    tmp_path: Path,
):
    """TH-D16: integration happens on task/<id>/integration, not in the base."""
    runner = ModeARunner()
    repo, run_dir, waiting = await _run_to_waiting_user(tmp_path, runner)
    assert waiting.status == "WAITING_USER"

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert state.stage == Stage.FINAL.value
    assert state.merge_status == "INTEGRATED"
    assert state.user_selection == "SELECT_WORKER_1"
    assert state.active_branch == "task/TASK-A/integration"
    # The integration branch is the task's line of work now, so a MODE B
    # relay can continue from it (TH-D15).
    assert state.active_worktree == state.worktrees.get("integration", {}).get(
        "path", state.active_worktree
    )
    assert len(state.checkpoints["integration"]) == 40
    assert state.checkpoint == state.checkpoints["integration"]
    assert state.test_status == CheckStatus.PASS.value

    merge_call = runner.calls[-1]
    assert merge_call["agent_type"] == "codex"
    assert merge_call["model"] == "gpt-5.6-terra"
    assert merge_call["effort"] == "high"
    assert merge_call["cwd"] != str(repo.resolve())

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert status == ""


async def test_the_merge_agent_is_shown_the_same_compare_report_as_the_human(
    tmp_path: Path,
):
    runner = ModeARunner()
    repo, run_dir, waiting = await _run_to_waiting_user(tmp_path, runner)
    report = Path(waiting.compare_path or "").read_text(encoding="utf-8")

    await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_HYBRID",
        user_instruction="take the better tests from each",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    merge_prompt = runner.calls[-1]["prompt"]
    assert "take the better tests from each" in merge_prompt
    # A distinctive line from the report reached the integrating agent.
    assert "## 선택지" in report
    assert "worker_1" in merge_prompt and "worker_2" in merge_prompt


async def test_cancel_ends_the_run_without_running_an_agent(tmp_path: Path):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    calls_before = len(runner.calls)

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="CANCEL",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == "CANCELLED"
    assert state.merge_status == "NOT_RUN"
    assert len(runner.calls) == calls_before


async def test_rework_parks_the_run_for_another_attempt(tmp_path: Path):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    calls_before = len(runner.calls)

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="REWORK",
        user_instruction="both missed the error path",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == "WAITING_REWORK"
    assert state.merge_status == "NOT_RUN"
    assert len(runner.calls) == calls_before


async def test_resume_rejects_an_invalid_selection_and_a_non_waiting_run(
    tmp_path: Path,
):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)

    invalid = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_3",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )
    assert invalid.status == StageStatus.BLOCKED.value
    assert "Invalid selection" in (invalid.blocker or "")

    # Now the run is BLOCKED, so a second resume is refused for that reason.
    again = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )
    assert again.status == StageStatus.BLOCKED.value
    assert "requires WAITING_USER" in (again.blocker or "")


async def test_resume_blocks_when_the_base_moved_since_the_run_started(tmp_path: Path):
    """The selection was made against a base that no longer exists."""
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    (repo / "later.txt").write_text("base moved on\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "base moved"], cwd=repo, check=True, capture_output=True
    )

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert "changed since MODE A started" in (state.blocker or "")


async def test_resume_blocks_when_a_selected_worker_has_no_checkpoint(tmp_path: Path):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    state_mgr = ProtocolStateManager(run_dir)
    saved = state_mgr.load_state()
    assert saved is not None
    saved.checkpoints["worker_1"] = ""
    state_mgr.save_state(saved)

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert "Missing checkpoint" in (state.blocker or "")


async def test_an_integration_that_changed_nothing_is_not_reported_integrated(
    tmp_path: Path,
):
    """TH-D3: a normal return from the agent is not proof of success."""
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    runner.merge_writes_file = False

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.merge_status != "INTEGRATED"
    assert "produced no changes" in (state.blocker or "")


async def test_resume_blocks_when_the_integration_agent_fails(tmp_path: Path):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    runner.merge_success = False
    runner.merge_error = "codex exited with code 2"

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_2",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.CODEX_MERGE.value] == StageStatus.BLOCKED.value
    assert state.blocker == "codex exited with code 2"
    assert state.merge_status != "INTEGRATED"


async def test_a_refused_integration_records_no_effective_model(tmp_path: Path):
    """TH-D18/TH-D6: a stage that never launched claims no model."""
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)
    runner.merge_success = False
    runner.merge_spawned = False
    runner.merge_error = "family 'codex' is rate limited; worker not launched"
    runner.merge_failure_classification = {
        "category": "rate_limit",
        "family": "codex",
        "resets_at": "2026-07-23T13:00:00+00:00",
    }

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
    )

    assert state.status == StageStatus.BLOCKED.value
    handoff = state.handoffs[-1]
    assert handoff["stage"] == Stage.CODEX_MERGE.value
    assert handoff["spawned"] is False
    assert handoff["effective_model"] is None
    assert handoff["requested_model"] == "gpt-5.6-terra"
    assert handoff["failure_classification"]["category"] == "rate_limit"


async def test_resume_blocks_when_the_integration_fails_its_checks(tmp_path: Path):
    runner = ModeARunner()
    repo, run_dir, _ = await _run_to_waiting_user(tmp_path, runner)

    state = await resume_mode_a(
        task_id="TASK-A",
        selection="SELECT_WORKER_1",
        user_instruction="",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=_CHEAP_CONFIG,
        check_commands=_FAILING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.CHECK.value] == StageStatus.BLOCKED.value
    assert "Integration checks failed" in (state.blocker or "")
    # The integration itself did happen — only the gate after it failed.
    assert state.merge_status == "INTEGRATED"


async def test_resume_requires_a_saved_state(tmp_path: Path):
    repo, _ = _repo(tmp_path)

    with pytest.raises(ValueError, match="No protocol state found"):
        await resume_mode_a(
            task_id="TASK-A",
            selection="SELECT_WORKER_1",
            user_instruction="",
            run_dir=tmp_path / "missing",
            target_repo=str(repo),
            agent_runner=ModeARunner(),
            protocol_config=_CHEAP_CONFIG,
        )


async def test_legacy_selection_aliases_still_pick_worker_1_and_2(tmp_path: Path):
    for selection, expected_branch_worker in (
        ("SELECT_CODEX", "worker_1"),
        ("SELECT_GEMINI", "worker_2"),
    ):
        runner = ModeARunner()
        repo, run_dir, waiting = await _run_to_waiting_user(
            tmp_path / selection, runner
        )
        state = await resume_mode_a(
            task_id="TASK-A",
            selection=selection,
            user_instruction="",
            run_dir=run_dir,
            target_repo=str(repo),
            agent_runner=runner,
            protocol_config=_CHEAP_CONFIG,
            check_commands=_PASSING_CHECK,
            auto_discover_checks=False,
        )
        assert state.status == StageStatus.DONE.value, selection
        assert state.user_selection == selection
        # The chosen lane's branch and checkpoint are named for the agent.
        assert (
            waiting.worker_branches[expected_branch_worker]
            in runner.calls[-1]["prompt"]
        )
