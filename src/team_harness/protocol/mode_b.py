"""MODE B — RELAY state machine.

Hands off an in-progress task to a different agent.
Does NOT create new worktrees — continues on the existing work branch.
"""

from __future__ import annotations

from pathlib import Path

from team_harness.protocol.checks import CheckStatus
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.git import relay_evidence
from team_harness.protocol.mode_c import _block
from team_harness.protocol.mode_c import _run_checks
from team_harness.protocol.mode_c import AgentRunner
from team_harness.protocol.models import normalize_agent_type
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.prompt import build_relay_prompt
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import checkpoint_worktree


async def run_mode_b(
    *,
    task_id: str,
    next_agent: str | None = None,
    run_dir: str | Path,
    target_repo: str,
    agent_runner: AgentRunner,
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
    check_commands: list[list[str]] | None = None,
    auto_discover_checks: bool = True,
    protocol_config: ProtocolConfig | None = None,
) -> ProtocolState:
    """Resume a task by relaying to a different agent.

    State machine:
        (existing task) → GIT_VERIFY → CONTINUE → CHECK → FINAL
    """
    proto_cfg = protocol_config or load_protocol_config()
    if next_agent is not None:
        target_agent = normalize_agent_type(next_agent)
        target_model = None
    else:
        target_agent = normalize_agent_type(proto_cfg.mode_b_default.agent_type)
        target_model = proto_cfg.mode_b_default.model
    resolved_type = target_agent

    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = state_mgr.load_state()
    if state is None:
        raise ValueError(f"No protocol state found in {run_path}")

    # Capture where the previous run actually stopped *before* GIT_VERIFY
    # below overwrites state.stage — this is what the receiving agent needs
    # to know ("pick up from here"), not MODE B's own current stage.
    stage_at_handoff = state.stage or "(unknown — no prior stage recorded)"

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

    if state.active_branch and evidence.branch != state.active_branch:
        discrepancies.append(
            f"recorded branch {state.active_branch} != Git branch {evidence.branch}"
        )

    expected_checkpoint = (
        state.relay.get("checkpoint")
        or state.checkpoints.get("relay")
        or state.checkpoints.get("pipeline")
    )
    if expected_checkpoint and evidence.head != expected_checkpoint:
        discrepancies.append(
            f"recorded checkpoint {expected_checkpoint} != "
            f"Git HEAD {evidence.head}; actual Git HEAD used"
        )

    state.git_discrepancies.extend(discrepancies)

    # Update relay info
    previous_agent = state.relay.get("current_agent") or state.logical_agent
    state.mode = "B"
    state.relay.update(
        {
            "previous_agent": previous_agent,
            "next_agent": target_agent,
            "worktree": str(worktree_path),
            "branch": evidence.branch,
            "checkpoint": evidence.head,
            "recent_log": evidence.recent_log,
            "diff_stat": evidence.diff_stat,
            "remaining_work": stage_at_handoff,
        }
    )

    state.previous_agent = previous_agent
    state.next_agent = target_agent
    state.logical_agent = target_agent
    state.backend_agent = resolved_type

    state.stage_statuses[Stage.GIT_VERIFY.value] = StageStatus.DONE.value
    state_mgr.append_event(
        "relay.verified",
        previous_agent=previous_agent,
        next_agent=target_agent,
        discrepancies=discrepancies,
    )

    # -- CONTINUE --
    state.stage = Stage.CONTINUE.value
    state.status = StageStatus.RUNNING.value
    state.stage_statuses[Stage.CONTINUE.value] = StageStatus.RUNNING.value
    state.blocker = None
    state_mgr.save_state(state)
    state_mgr.append_event(
        "relay.continue", agent=target_agent, worktree=str(worktree_path)
    )

    prompt = build_relay_prompt(
        task_id=task_id,
        previous_agent=previous_agent,
        remaining_work=stage_at_handoff,
        branch=evidence.branch,
        checkpoint=evidence.head,
        recent_log=evidence.recent_log,
        diff_stat=evidence.diff_stat,
        changed_files=evidence.changed_files,
    )

    result = await agent_runner.run_agent(
        agent_type=resolved_type,
        prompt=prompt,
        cwd=str(worktree_path),
        timeout_sec=agent_timeout_sec,
        model=target_model,
        label=f"{target_agent}-relay",
    )
    effective_model = (
        result.effective_model
        if result.effective_model is not None
        else (result.model if result.model is not None else target_model)
    )
    result.agent = target_agent
    result.agent_type = target_agent
    result.stage = Stage.CONTINUE.value
    result.model = effective_model
    result.requested_model = target_model
    result.effective_model = effective_model

    state.handoffs.append(
        {
            "stage": Stage.CONTINUE.value,
            "agent": target_agent,
            "agent_type": target_agent,
            "model": effective_model,
            "requested_model": target_model,
            "effective_model": effective_model,
            "success": result.success,
        }
    )
    if result.output_text:
        state_mgr.write_output(Stage.CONTINUE.value, target_agent, result.output_text)

    if not result.success:
        return _block(
            state,
            state_mgr,
            Stage.CONTINUE.value,
            result.error_message or f"Relay agent {target_agent} failed",
        )

    state.stage_statuses[Stage.CONTINUE.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- CHECK --
    check_status = _run_checks(
        state=state,
        state_mgr=state_mgr,
        worktree_path=str(worktree_path),
        check_commands=check_commands,
        auto_discover=auto_discover_checks,
        timeout_sec=check_timeout_sec,
    )
    if check_status == CheckStatus.FAIL.value:
        return _block(state, state_mgr, Stage.CHECK.value, "Relay checks failed")

    # Checkpoint whatever the relay agent changed. No new changes (an agent
    # that only investigated, or already-committed work) is not a failure —
    # match mode_a.py's handling of the same "nothing to commit" case.
    try:
        new_checkpoint = checkpoint_worktree(worktree_path, task_id, "relay")
    except RuntimeError:
        new_checkpoint = evidence.head
    state.checkpoint = new_checkpoint
    state.checkpoints["relay"] = new_checkpoint
    state.relay["checkpoint"] = new_checkpoint

    # -- FINAL --
    state.stage = Stage.FINAL.value
    state.stage_statuses[Stage.FINAL.value] = StageStatus.DONE.value
    state.status = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event(
        "run.finished", status="DONE", mode="B", checkpoint=new_checkpoint
    )

    return state
