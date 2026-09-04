"""MODE B — RELAY state machine.

Hands off an in-progress task to a different agent.
Does NOT create new worktrees — continues on the existing work branch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from team_harness.protocol.git import git_preflight, relay_evidence
from team_harness.protocol.models import (
    ProtocolState,
    Stage,
    StageStatus,
    resolve_agent_type,
)
from team_harness.protocol.mode_c import AgentRunner, _block
from team_harness.protocol.state import ProtocolStateManager


async def run_mode_b(
    *,
    task_id: str,
    next_agent: str,
    run_dir: str | Path,
    target_repo: str,
    agent_runner: AgentRunner,
    agent_timeout_sec: int = 600,
) -> ProtocolState:
    """Resume a task by relaying to a different agent.

    State machine:
        (existing task) → GIT_VERIFY → CONTINUE
    """
    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = state_mgr.load_state()
    if state is None:
        raise ValueError(f"No protocol state found in {run_path}")

    if next_agent not in ("claude", "codex", "gemini"):
        raise ValueError(f"Invalid relay agent: {next_agent}")

    # -- GIT_VERIFY --
    state.stage = Stage.GIT_VERIFY.value
    state.stage_statuses[Stage.GIT_VERIFY.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    # Must have an active worktree
    if not state.active_worktree:
        return _block(
            state, state_mgr, Stage.GIT_VERIFY.value, "No active worktree in state"
        )

    worktree_path = Path(state.active_worktree).resolve()
    base_repo = Path(target_repo).resolve()

    if worktree_path == base_repo:
        return _block(
            state,
            state_mgr,
            Stage.GIT_VERIFY.value,
            "MODE B cannot write in base worktree",
        )

    # Verify worktree exists and gather evidence
    try:
        evidence = relay_evidence(str(worktree_path))
    except Exception as exc:
        return _block(state, state_mgr, Stage.GIT_VERIFY.value, str(exc))

    # Detect discrepancies between recorded and actual state
    discrepancies: list[str] = []

    if state.active_branch and evidence["branch"] != state.active_branch:
        discrepancies.append(
            f"recorded branch {state.active_branch} != Git branch {evidence['branch']}"
        )

    expected_checkpoint = (
        state.relay.get("checkpoint")
        or state.checkpoints.get("relay")
        or state.checkpoints.get("pipeline")
    )
    if expected_checkpoint and evidence["head"] != expected_checkpoint:
        discrepancies.append(
            f"recorded checkpoint {expected_checkpoint} != "
            f"Git HEAD {evidence['head']}; actual Git HEAD used"
        )

    state.git_discrepancies.extend(discrepancies)

    # Update relay info
    previous_agent = state.relay.get("current_agent") or state.logical_agent
    state.mode = "B"
    state.relay.update({
        "previous_agent": previous_agent,
        "next_agent": next_agent,
        "worktree": str(worktree_path),
        "branch": evidence["branch"],
        "checkpoint": evidence["head"],
        "recent_log": evidence["recent_log"],
        "diff_stat": evidence["diff_stat"],
        "remaining_work": state.stage or "first incomplete stage",
    })

    state.previous_agent = previous_agent
    state.next_agent = next_agent
    state.logical_agent = next_agent
    state.backend_agent = resolve_agent_type(next_agent)

    state.stage_statuses[Stage.GIT_VERIFY.value] = StageStatus.DONE.value
    state_mgr.append_event(
        "relay.verified",
        previous_agent=previous_agent,
        next_agent=next_agent,
        discrepancies=discrepancies,
    )

    # -- CONTINUE --
    state.stage = Stage.CONTINUE.value
    state.status = StageStatus.RUNNING.value
    state.stage_statuses[Stage.CONTINUE.value] = StageStatus.RUNNING.value
    state.blocker = None
    state_mgr.save_state(state)
    state_mgr.append_event(
        "relay.continue",
        agent=next_agent,
        worktree=str(worktree_path),
    )

    # In a full implementation, this would resume the remaining pipeline stages.
    # For now, we mark the relay as set up and ready.
    state.stage_statuses[Stage.CONTINUE.value] = StageStatus.DONE.value
    state.status = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("run.finished", status="DONE", mode="B")

    return state
