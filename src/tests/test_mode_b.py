"""MODE B — RELAY: handing an in-progress task to a different agent.

MODE B is the mode that must never start over: it continues on the branch and
worktree a previous run left behind, so almost everything worth testing is
about trusting Git over the recorded state and refusing to touch the base repo.
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from typing import Any

import pytest

from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.mode_b import run_mode_b
from team_harness.protocol.models import AgentResult
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import create_worktree

from .test_mode_c import _init_git_repo

_PASSING_CHECK = [[sys.executable, "-c", "print('ok')"]]
_FAILING_CHECK = [[sys.executable, "-c", "raise SystemExit(1)"]]


class RelayRunner:
    """Records what MODE B asked for, and can be made to fail or be refused."""

    def __init__(
        self,
        *,
        success: bool = True,
        output_text: str = "Relay agent continued the work.",
        error_message: str | None = None,
        spawned: bool = True,
        failure_classification: dict[str, Any] | None = None,
        file_to_write: tuple[str, str] | None = ("relayed.py", "x = 1\n"),
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.success = success
        self.output_text = output_text
        self.error_message = error_message
        self.spawned = spawned
        self.failure_classification = failure_classification
        self.file_to_write = file_to_write

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
        if self.success and self.file_to_write:
            rel_path, content = self.file_to_write
            (Path(cwd) / rel_path).write_text(content, encoding="utf-8")
        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            stage=Stage.CONTINUE.value,
            success=self.success,
            output_text=self.output_text if self.success else "",
            error_message=self.error_message,
            spawned=self.spawned,
            failure_classification=self.failure_classification,
        )


def _relay_fixture(
    tmp_path: Path,
    *,
    stage: str = "IMPLEMENT",
    logical_agent: str | None = "codex",
    commit_in_worktree: bool = True,
):
    """A repo plus a task worktree with saved state, ready to be relayed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_git_repo(repo)

    wt = create_worktree(
        repo,
        "TASK-RELAY",
        "pipeline",
        base_commit,
        worktrees_base_dir=tmp_path / "worktrees",
    )
    if commit_in_worktree:
        (Path(wt.path) / "progress.py").write_text("half = True\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "-A"], cwd=wt.path, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-m", "work in progress"],
            cwd=wt.path,
            check=True,
            capture_output=True,
        )

    run_dir = tmp_path / "run"
    state_mgr = ProtocolStateManager(run_dir)
    state_mgr.save_state(
        ProtocolState(
            task_id="TASK-RELAY",
            mode="C",
            stage=stage,
            status=StageStatus.BLOCKED.value,
            target_repo=str(repo),
            base_commit=base_commit,
            logical_agent=logical_agent,
            active_worktree=wt.path,
            active_branch=wt.branch,
            worktrees={
                "pipeline": {
                    "label": wt.label,
                    "branch": wt.branch,
                    "path": wt.path,
                    "base_commit": wt.base_commit,
                }
            },
        )
    )
    return repo, run_dir, wt


async def test_relay_happy_path_continues_in_the_existing_worktree(tmp_path: Path):
    repo, run_dir, wt = _relay_fixture(tmp_path)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert state.stage == Stage.FINAL.value
    assert state.mode == "B"
    assert state.test_status == CheckStatus.PASS.value
    # It ran in the existing task worktree, never the base repo, and created
    # no new worktree of its own.
    assert runner.calls[0]["cwd"] == wt.path
    assert runner.calls[0]["cwd"] != str(repo.resolve())
    assert list(state.worktrees) == ["pipeline"]
    # The relay agent's work is checkpointed onto the same branch.
    assert state.checkpoints["relay"] == state.checkpoint
    assert state.relay["checkpoint"] == state.checkpoint


async def test_relay_tells_the_receiver_where_the_previous_run_stopped(tmp_path: Path):
    """The handoff point is the stage that stopped, not MODE B's own stage."""
    repo, run_dir, _ = _relay_fixture(tmp_path, stage="REVIEW")
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.relay["remaining_work"] == "REVIEW"
    assert "REVIEW" in runner.calls[0]["prompt"]
    # GIT_VERIFY/CONTINUE must not have leaked into the handoff description.
    assert state.relay["remaining_work"] != Stage.GIT_VERIFY.value
    assert state.previous_agent == "codex"
    assert state.next_agent == "claude"


async def test_relay_records_previous_agent_even_without_one(tmp_path: Path):
    repo, run_dir, _ = _relay_fixture(tmp_path, logical_agent=None)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert state.previous_agent is None


async def test_relay_without_next_agent_uses_the_configured_default(tmp_path: Path):
    """The default carries model *and* effort, which must reach the worker."""
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner()
    proto_cfg = ProtocolConfig(
        mode_b_default=ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )

    state = await run_mode_b(
        task_id="TASK-RELAY",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=proto_cfg,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert runner.calls[0]["agent_type"] == "codex"
    assert runner.calls[0]["model"] == "gpt-5.6-terra"
    assert runner.calls[0]["effort"] == "high"
    assert state.handoffs[-1]["requested_model"] == "gpt-5.6-terra"
    assert state.handoffs[-1]["effective_model"] == "gpt-5.6-terra"


async def test_an_explicit_next_agent_does_not_inherit_the_defaults_model(
    tmp_path: Path,
):
    """Naming a different agent must not send the default agent's model to it."""
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner()
    proto_cfg = ProtocolConfig(
        mode_b_default=ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )

    await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=proto_cfg,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert runner.calls[0]["agent_type"] == "claude"
    assert runner.calls[0]["model"] is None
    assert runner.calls[0]["effort"] is None


async def test_gemini_alias_is_normalized_to_the_antigravity_backend(tmp_path: Path):
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="gemini",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert runner.calls[0]["agent_type"] == "antigravity"
    assert state.next_agent == "antigravity"


async def test_relay_blocks_when_no_active_worktree_is_recorded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    run_dir = tmp_path / "run"
    ProtocolStateManager(run_dir).save_state(
        ProtocolState(task_id="TASK-RELAY", mode="C", stage="IMPLEMENT")
    )
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.GIT_VERIFY.value] == StageStatus.BLOCKED.value
    assert "No active worktree" in (state.blocker or "")
    assert runner.calls == []


async def test_relay_refuses_to_work_in_the_base_worktree(tmp_path: Path):
    """TH-D16: protocol runs never modify the base repository's working tree."""
    repo = tmp_path / "repo"
    repo.mkdir()
    base_commit = _init_git_repo(repo)
    run_dir = tmp_path / "run"
    ProtocolStateManager(run_dir).save_state(
        ProtocolState(
            task_id="TASK-RELAY",
            mode="C",
            stage="IMPLEMENT",
            base_commit=base_commit,
            active_worktree=str(repo),
        )
    )
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert "cannot write in base worktree" in (state.blocker or "")
    assert runner.calls == []


async def test_relay_blocks_when_the_recorded_worktree_is_gone(tmp_path: Path):
    repo, run_dir, wt = _relay_fixture(tmp_path)
    import shutil

    shutil.rmtree(wt.path)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.GIT_VERIFY.value] == StageStatus.BLOCKED.value
    assert runner.calls == []


async def test_relay_trusts_git_over_a_stale_recorded_branch_and_checkpoint(
    tmp_path: Path,
):
    """Recorded state can be stale; Git is the truth, and the gap is logged."""
    repo, run_dir, wt = _relay_fixture(tmp_path)
    state_mgr = ProtocolStateManager(run_dir)
    saved = state_mgr.load_state()
    assert saved is not None
    saved.active_branch = "task/TASK-RELAY/some-other-branch"
    saved.checkpoints["pipeline"] = "0" * 40
    state_mgr.save_state(saved)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert len(state.git_discrepancies) == 2
    assert any("!= Git branch" in item for item in state.git_discrepancies)
    assert any("actual Git HEAD used" in item for item in state.git_discrepancies)
    # The prompt describes the real branch, not the stale recorded one.
    assert wt.branch in runner.calls[0]["prompt"]
    assert state.relay["branch"] == wt.branch


async def test_relay_blocks_when_the_receiving_agent_fails(tmp_path: Path):
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner(success=False, error_message="claude exited with code 1")

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.CONTINUE.value] == StageStatus.BLOCKED.value
    assert state.blocker == "claude exited with code 1"
    assert state.handoffs[-1]["success"] is False


async def test_a_refused_relay_records_no_effective_model(tmp_path: Path):
    """TH-D18/TH-D6: nothing ran, so no model may be claimed in the audit."""
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner(
        success=False,
        spawned=False,
        error_message="family 'codex' is rate limited; worker not launched",
        failure_classification={"category": "rate_limit", "family": "codex"},
    )
    proto_cfg = ProtocolConfig(
        mode_b_default=ProtocolAgentSpec(
            agent_type="codex", model="gpt-5.6-terra", effort="high"
        )
    )

    state = await run_mode_b(
        task_id="TASK-RELAY",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        protocol_config=proto_cfg,
    )

    assert state.status == StageStatus.BLOCKED.value
    handoff = state.handoffs[-1]
    assert handoff["spawned"] is False
    assert handoff["effective_model"] is None
    assert handoff["requested_model"] == "gpt-5.6-terra"
    assert handoff["failure_classification"]["category"] == "rate_limit"


async def test_relay_blocks_when_checks_fail_after_a_successful_continue(
    tmp_path: Path,
):
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner()

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_FAILING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.BLOCKED.value
    assert state.stage_statuses[Stage.CONTINUE.value] == StageStatus.DONE.value
    assert state.stage_statuses[Stage.CHECK.value] == StageStatus.BLOCKED.value
    assert "checks failed" in (state.blocker or "")


async def test_an_investigation_only_relay_keeps_the_existing_head(tmp_path: Path):
    """An agent that changed nothing is not a failure — the HEAD just stands."""
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner(file_to_write=None)

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.status == StageStatus.DONE.value
    assert state.checkpoint == state.relay["checkpoint"]
    assert len(state.checkpoint or "") == 40


async def test_relay_requires_a_saved_state(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    with pytest.raises(ValueError, match="No protocol state found"):
        await run_mode_b(
            task_id="TASK-RELAY",
            next_agent="claude",
            run_dir=tmp_path / "missing",
            target_repo=str(repo),
            agent_runner=RelayRunner(),
        )


async def test_relay_persists_its_output_and_clears_the_previous_blocker(
    tmp_path: Path,
):
    repo, run_dir, _ = _relay_fixture(tmp_path)
    runner = RelayRunner(output_text="Finished the remaining work.")

    state = await run_mode_b(
        task_id="TASK-RELAY",
        next_agent="claude",
        run_dir=run_dir,
        target_repo=str(repo),
        agent_runner=runner,
        check_commands=_PASSING_CHECK,
        auto_discover_checks=False,
    )

    assert state.blocker is None
    output = run_dir / "protocol_outputs" / Stage.CONTINUE.value / "claude.md"
    assert output.read_text(encoding="utf-8") == "Finished the remaining work."
    persisted = ProtocolStateManager(run_dir).load_state()
    assert persisted is not None
    assert persisted.status == StageStatus.DONE.value
    assert persisted.mode == "B"
