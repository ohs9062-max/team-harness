"""MODE A — PARALLEL COMPETITION state machine.

Two workers (Codex + Gemini/antigravity) operate in independent worktrees.
After cross-review and response, the runner waits for user selection.
"""

from __future__ import annotations

import json
from pathlib import Path
import re

from team_harness.protocol.checks import CheckRunner
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.git import git_preflight
from team_harness.protocol.mode_c import _block
from team_harness.protocol.mode_c import _run_checks
from team_harness.protocol.mode_c import AgentRunner
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import resolve_agent_type
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.models import UserSelection
from team_harness.protocol.prompt import build_cross_review_prompt
from team_harness.protocol.prompt import build_independent_work_prompt
from team_harness.protocol.prompt import build_merge_prompt
from team_harness.protocol.prompt import build_response_prompt
from team_harness.protocol.state import mask_sensitive
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import changed_files
from team_harness.protocol.worktree import checkpoint_worktree
from team_harness.protocol.worktree import create_worktree
from team_harness.protocol.worktree import diff_worktree
from team_harness.protocol.worktree import verify_checkpoint


async def run_mode_a(
    *,
    task_id: str,
    user_request: str,
    target_repo: str,
    run_dir: str | Path,
    agent_runner: AgentRunner,
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
    check_commands: list[list[str]] | None = None,
    auto_discover_checks: bool = True,
    protocol_config: ProtocolConfig | None = None,
) -> ProtocolState:
    """Execute MODE A: PARALLEL COMPETITION.

    State machine:
        DEFINE → GIT_PREFLIGHT → BASE_FREEZE → WORKTREE_SETUP
        → INDEPENDENT_WORK (two workers in parallel worktrees)
        → WORKER_GATE → CROSS_REVIEW → RESPONSE (1 round)
        → COMPARE → WAITING_USER
    """
    proto_cfg = protocol_config or load_protocol_config()
    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = ProtocolState(
        task_id=task_id, mode="A", user_request=user_request, target_repo=target_repo
    )
    workers = ("worker_1", "worker_2")
    worker_specs = {
        "worker_1": proto_cfg.mode_a_worker_1,
        "worker_2": proto_cfg.mode_a_worker_2,
    }

    # -- DEFINE --
    state.stage = Stage.DEFINE.value
    state.status = StageStatus.RUNNING.value
    state.stage_statuses[Stage.DEFINE.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- GIT_PREFLIGHT --
    state.stage = Stage.GIT_PREFLIGHT.value
    state.stage_statuses[Stage.GIT_PREFLIGHT.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    try:
        preflight = git_preflight(target_repo)
    except Exception as exc:
        return _block(state, state_mgr, Stage.GIT_PREFLIGHT.value, str(exc))

    state.base_branch = preflight.branch
    state.base_commit = preflight.head
    if preflight.dirty:
        return _block(
            state,
            state_mgr,
            Stage.GIT_PREFLIGHT.value,
            f"Repository has uncommitted changes: {preflight.status_lines[:5]}",
        )
    state.stage_statuses[Stage.GIT_PREFLIGHT.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- WORKTREE_SETUP (both workers from same frozen base) --
    state.stage = Stage.WORKTREE_SETUP.value
    state.stage_statuses[Stage.WORKTREE_SETUP.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    for worker in workers:
        try:
            wt = create_worktree(target_repo, task_id, worker, state.base_commit)
        except Exception as exc:
            return _block(state, state_mgr, Stage.WORKTREE_SETUP.value, str(exc))
        state.worktrees[worker] = {
            "label": wt.label,
            "branch": wt.branch,
            "path": wt.path,
            "base_commit": wt.base_commit,
        }
        state.worker_branches[worker] = wt.branch
        state.worker_status[worker] = StageStatus.PENDING.value
        state_mgr.append_event(
            "worktree.created",
            label=worker,
            path=wt.path,
            branch=wt.branch,
            base_commit=state.base_commit,
        )

    state.stage_statuses[Stage.WORKTREE_SETUP.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- INDEPENDENT_WORK --
    state.stage = Stage.INDEPENDENT_WORK.value
    for worker in workers:
        wt_info = state.worktrees[worker]
        wt_path = wt_info["path"]
        state.worker_status[worker] = StageStatus.RUNNING.value
        state_mgr.save_state(state)

        worker_spec = worker_specs[worker]
        agent_type = resolve_agent_type(worker_spec.agent_type)
        prompt = build_independent_work_prompt(
            task_id=task_id,
            user_request=user_request,
            agent=worker,
            worktree_path=wt_path,
            base_commit=state.base_commit,
        )
        result = await agent_runner.run_agent(
            agent_type=agent_type,
            prompt=prompt,
            cwd=wt_path,
            timeout_sec=agent_timeout_sec,
            model=worker_spec.model,
        )
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else worker_spec.model)
        )
        result.agent = worker
        result.agent_type = agent_type
        result.stage = Stage.INDEPENDENT_WORK.value
        result.model = effective_model
        result.requested_model = worker_spec.model
        result.effective_model = effective_model

        state.handoffs.append(
            {
                "stage": Stage.INDEPENDENT_WORK.value,
                "lane": worker,
                "agent": worker,
                "agent_type": agent_type,
                "model": effective_model,
                "requested_model": worker_spec.model,
                "effective_model": effective_model,
                "success": result.success,
            }
        )

        if not result.success:
            state.worker_status[worker] = StageStatus.FAILED.value
            state.blocker = result.error_message or f"Worker {worker} failed"
            return _block(
                state,
                state_mgr,
                Stage.WORKER_GATE.value,
                f"Required MODE A worker failed: {worker}",
            )

        # Run checks in worker worktree
        check_runner = CheckRunner(wt_path, timeout_sec=check_timeout_sec)
        cmds: list[list[str]] = list(check_commands or [])
        if auto_discover_checks:
            cmds.extend(check_runner.discover())
        unique_cmds: list[list[str]] = []
        for cmd in cmds:
            if cmd not in unique_cmds:
                unique_cmds.append(cmd)
        worker_checks = (
            [r.to_dict() for r in check_runner.run_all(unique_cmds)]
            if unique_cmds
            else []
        )
        state.worker_tests[worker] = worker_checks

        if any(not c["success"] for c in worker_checks):
            state.worker_status[worker] = StageStatus.FAILED.value
            return _block(
                state,
                state_mgr,
                Stage.WORKER_GATE.value,
                f"Required worker check failed: {worker}",
            )

        # Checkpoint
        try:
            cp = checkpoint_worktree(wt_path, task_id, worker)
            state.checkpoints[worker] = cp
        except RuntimeError:
            # Mock agents may not produce changes
            state.checkpoints[worker] = ""

        state.worker_status[worker] = StageStatus.DONE.value
        state_mgr.append_event(
            "worker.finished",
            lane=worker,
            agent=worker,
            agent_type=agent_type,
            model=effective_model,
            requested_model=worker_spec.model,
            effective_model=effective_model,
            checkpoint=state.checkpoints.get(worker, ""),
        )
        state_mgr.save_state(state)

    state.stage_statuses[Stage.INDEPENDENT_WORK.value] = StageStatus.DONE.value
    state.stage_statuses[Stage.WORKER_GATE.value] = StageStatus.DONE.value

    # -- CROSS_REVIEW --
    state.stage = Stage.CROSS_REVIEW.value
    state.stage_statuses[Stage.CROSS_REVIEW.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    pairs = [(workers[1], workers[0]), (workers[0], workers[1])]
    for reviewer, target in pairs:
        target_wt = state.worktrees[target]["path"]
        target_cp = state.checkpoints.get(target, "")

        try:
            diff_text = (
                diff_worktree(state.base_commit, target_cp, target_wt)
                if target_cp
                else ""
            )
        except Exception:
            diff_text = ""

        try:
            changed = (
                changed_files(state.base_commit, target_cp, target_wt)
                if target_cp
                else []
            )
        except Exception:
            changed = []

        test_summary = mask_sensitive(
            json.dumps(state.worker_tests.get(target, []), ensure_ascii=False)
        )[:20_000]

        reviewer_spec = worker_specs[reviewer]
        reviewer_type = resolve_agent_type(reviewer_spec.agent_type)
        prompt = build_cross_review_prompt(
            task_id=task_id,
            user_request=user_request,
            reviewer=reviewer,
            target=target,
            diff_text=diff_text,
            changed_files=changed,
            test_results=test_summary,
            base_commit=state.base_commit,
            target_branch=state.worker_branches.get(target, ""),
            target_checkpoint=target_cp,
        )

        result = await agent_runner.run_agent(
            agent_type=reviewer_type,
            prompt=prompt,
            cwd=target_wt,
            timeout_sec=agent_timeout_sec,
            model=reviewer_spec.model,
        )
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else reviewer_spec.model)
        )
        result.agent = reviewer
        result.agent_type = reviewer_type
        result.stage = Stage.CROSS_REVIEW.value
        result.model = effective_model
        result.requested_model = reviewer_spec.model
        result.effective_model = effective_model

        state.handoffs.append(
            {
                "stage": Stage.CROSS_REVIEW.value,
                "lane": reviewer,
                "agent": reviewer,
                "agent_type": reviewer_type,
                "target": target,
                "model": effective_model,
                "requested_model": reviewer_spec.model,
                "effective_model": effective_model,
                "success": result.success,
            }
        )

        key = f"{reviewer}_reviews_{target}"
        state.cross_reviews[key] = {
            "reviewer": reviewer,
            "target": target,
            "reviewer_agent": reviewer_type,
            "model": effective_model,
            "requested_model": reviewer_spec.model,
            "effective_model": effective_model,
            "success": result.success,
            "output_text": result.output_text,
        }

        if result.output_text:
            state_mgr.write_output(f"CROSS_REVIEW_{key}", reviewer, result.output_text)

        if not result.success:
            return _block(
                state,
                state_mgr,
                Stage.CROSS_REVIEW.value,
                f"Cross review failed: {key}",
            )

    state.stage_statuses[Stage.CROSS_REVIEW.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- RESPONSE (1 round) --
    state.stage = Stage.RESPONSE.value
    state.stage_statuses[Stage.RESPONSE.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    for worker, reviewer in [(workers[0], workers[1]), (workers[1], workers[0])]:
        review_key = f"{reviewer}_reviews_{worker}"
        review_text = state.cross_reviews.get(review_key, {}).get("output_text", "")

        worker_spec = worker_specs[worker]
        worker_type = resolve_agent_type(worker_spec.agent_type)
        prompt = build_response_prompt(
            task_id=task_id,
            worker=worker,
            checkpoint=state.checkpoints.get(worker, ""),
            review_findings=review_text,
        )

        result = await agent_runner.run_agent(
            agent_type=worker_type,
            prompt=prompt,
            cwd=state.worktrees[worker]["path"],
            timeout_sec=agent_timeout_sec,
            model=worker_spec.model,
        )
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else worker_spec.model)
        )
        result.agent = worker
        result.agent_type = worker_type
        result.stage = Stage.RESPONSE.value
        result.model = effective_model
        result.requested_model = worker_spec.model
        result.effective_model = effective_model

        dispositions = re.findall(
            r"\b(?:ACCEPT|REJECT|PARTIAL|NEEDS_TEST)\b", result.output_text
        )
        state.responses[worker] = {
            "round": 1,
            "lane": worker,
            "agent": worker_type,
            "model": effective_model,
            "requested_model": worker_spec.model,
            "effective_model": effective_model,
            "success": result.success,
            "dispositions": dispositions,
            "output_text": result.output_text,
        }
        state.handoffs.append(
            {
                "stage": Stage.RESPONSE.value,
                "lane": worker,
                "agent": worker,
                "agent_type": worker_type,
                "model": effective_model,
                "requested_model": worker_spec.model,
                "effective_model": effective_model,
                "success": result.success,
            }
        )

        if result.output_text:
            state_mgr.write_output(f"RESPONSE_{worker}", worker, result.output_text)

        if not result.success or not dispositions:
            return _block(
                state,
                state_mgr,
                Stage.RESPONSE.value,
                f"Invalid MODE A response from {worker}",
            )

    state.stage_statuses[Stage.RESPONSE.value] = StageStatus.DONE.value
    state_mgr.save_state(state)

    # -- COMPARE & WAITING_USER --
    state.stage = Stage.COMPARE.value
    state.stage_statuses[Stage.COMPARE.value] = StageStatus.DONE.value
    state.stage = Stage.WAITING_USER.value
    state.status = "WAITING_USER"
    state_mgr.save_state(state)
    state_mgr.append_event(
        "selection.required",
        options=[
            "SELECT_WORKER_1",
            "SELECT_WORKER_2",
            "SELECT_HYBRID",
            "REWORK",
            "CANCEL",
        ],
    )

    return state


async def resume_mode_a(
    *,
    task_id: str,
    selection: str,
    user_instruction: str,
    run_dir: str | Path,
    target_repo: str,
    agent_runner: AgentRunner,
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
    check_commands: list[list[str]] | None = None,
    auto_discover_checks: bool = True,
    protocol_config: ProtocolConfig | None = None,
) -> ProtocolState:
    """Resume MODE A after user selection.

    Valid selections: SELECT_CODEX, SELECT_GEMINI, SELECT_HYBRID, REWORK, CANCEL.
    """
    proto_cfg = protocol_config or load_protocol_config()
    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = state_mgr.load_state()
    if state is None:
        raise ValueError(f"No protocol state found in {run_path}")

    selection = selection.upper()
    valid = {s.value for s in UserSelection}
    if state.mode != "A" or state.status != "WAITING_USER":
        return _block(
            state,
            state_mgr,
            Stage.USER_SELECT.value,
            "MODE A selection requires WAITING_USER state",
        )
    if selection not in valid:
        return _block(
            state, state_mgr, Stage.USER_SELECT.value, f"Invalid selection: {selection}"
        )

    state.user_selection = selection
    state.stage_statuses[Stage.USER_SELECT.value] = StageStatus.DONE.value
    state_mgr.append_event("selection.recorded", selection=selection)

    if selection == UserSelection.CANCEL.value:
        state.status = "CANCELLED"
        state.merge_status = "NOT_RUN"
        state_mgr.save_state(state)
        return state

    if selection == UserSelection.REWORK.value:
        state.status = "WAITING_REWORK"
        state.merge_status = "NOT_RUN"
        state_mgr.save_state(state)
        return state

    # Verify base hasn't changed
    try:
        preflight = git_preflight(target_repo)
    except Exception as exc:
        return _block(state, state_mgr, Stage.CODEX_MERGE.value, str(exc))

    if (
        preflight.branch != state.base_branch
        or preflight.head != state.base_commit
        or preflight.dirty
    ):
        return _block(
            state,
            state_mgr,
            Stage.CODEX_MERGE.value,
            "Base branch/HEAD/dirty state changed since MODE A started",
        )

    # Verify checkpoints
    workers = (
        list(state.worktrees.keys()) if state.worktrees else ["worker_1", "worker_2"]
    )
    for worker in workers:
        cp = state.checkpoints.get(worker, "")
        if not cp:
            return _block(
                state,
                state_mgr,
                Stage.CODEX_MERGE.value,
                f"Missing checkpoint for {worker}",
            )
        wt_info = state.worktrees.get(worker, {})
        try:
            verify_checkpoint(
                wt_info.get("path", ""),
                state.worker_branches.get(worker, ""),
                cp,
                state.base_commit,
            )
        except Exception as exc:
            return _block(state, state_mgr, Stage.CODEX_MERGE.value, str(exc))

    # Determine selected workers
    if selection in (
        UserSelection.SELECT_WORKER_1.value,
        UserSelection.SELECT_CODEX.value,
    ):
        selected = [workers[0]] if len(workers) > 0 else []
    elif selection in (
        UserSelection.SELECT_WORKER_2.value,
        UserSelection.SELECT_GEMINI.value,
    ):
        selected = [workers[1]] if len(workers) > 1 else []
    else:
        selected = list(workers)

    selected_details = "\n".join(
        f"- {w}: branch={state.worker_branches.get(w, '')} "
        f"checkpoint={state.checkpoints.get(w, '')} "
        f"worktree={state.worktrees.get(w, {}).get('path', '')}"
        for w in selected
    )

    # -- CODEX_MERGE --
    state.stage = Stage.CODEX_MERGE.value
    state.stage_statuses[Stage.CODEX_MERGE.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    final_spec = proto_cfg.mode_a_final
    final_type = resolve_agent_type(final_spec.agent_type)
    prompt = build_merge_prompt(
        task_id=task_id,
        selection=selection,
        user_instruction=user_instruction,
        base_branch=state.base_branch,
        base_head=preflight.head,
        selected_details=selected_details,
        compare_text="",  # Compare report would be loaded from file in production
    )

    result = await agent_runner.run_agent(
        agent_type=final_type,
        prompt=prompt,
        cwd=target_repo,
        timeout_sec=agent_timeout_sec,
        model=final_spec.model,
    )
    effective_model = (
        result.effective_model
        if result.effective_model is not None
        else (result.model if result.model is not None else final_spec.model)
    )
    result.agent = final_type
    result.agent_type = final_type
    result.stage = Stage.CODEX_MERGE.value
    result.model = effective_model
    result.requested_model = final_spec.model
    result.effective_model = effective_model

    state.handoffs.append(
        {
            "stage": Stage.CODEX_MERGE.value,
            "agent": final_type,
            "model": effective_model,
            "requested_model": final_spec.model,
            "effective_model": effective_model,
            "success": result.success,
        }
    )

    if not result.success:
        return _block(
            state,
            state_mgr,
            Stage.CODEX_MERGE.value,
            result.error_message or f"Integration agent {final_type} failed",
        )

    state.merge_status = "INTEGRATED"
    state.stage_statuses[Stage.CODEX_MERGE.value] = StageStatus.DONE.value

    # -- CHECK --
    check_status = _run_checks(
        state=state,
        state_mgr=state_mgr,
        worktree_path=target_repo,
        check_commands=check_commands,
        auto_discover=auto_discover_checks,
        timeout_sec=check_timeout_sec,
    )
    if check_status == CheckStatus.FAIL.value:
        return _block(state, state_mgr, Stage.CHECK.value, "Integration checks failed")

    # -- FINAL --
    state.stage = Stage.FINAL.value
    state.stage_statuses[Stage.FINAL.value] = StageStatus.DONE.value
    state.status = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("run.finished", status="DONE", selection=selection)

    return state
