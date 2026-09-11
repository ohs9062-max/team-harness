"""MODE A — PARALLEL COMPETITION state machine.

Two workers operate in independent worktrees (defaults to Codex + Antigravity).
After cross-review and response, the runner waits for user selection.
"""

from __future__ import annotations

import asyncio
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


def _build_compare_report(state: ProtocolState) -> str:
    """Render everything gathered so far into one skimmable Markdown report.

    Every piece of content here already lives somewhere in `state` (worktree
    diffs, cross-review verdicts, response dispositions) — this only
    assembles it into the single document a person reads right before
    choosing SELECT_WORKER_1/SELECT_WORKER_2/SELECT_HYBRID/REWORK/CANCEL, so
    they don't have to open state.json to find it.
    """
    workers = tuple(state.worktrees.keys()) or ("worker_1", "worker_2")
    # state.handoffs already records agent_type per (stage, lane); reuse it
    # rather than threading worker_specs through as a separate parameter.
    agent_types = {
        h["lane"]: h.get("agent_type", "?")
        for h in state.handoffs
        if h.get("stage") == Stage.INDEPENDENT_WORK.value and "lane" in h
    }
    lines: list[str] = [
        f"# MODE A 비교 리포트 — {state.task_id}",
        "",
        f"**작업 요청:** {state.user_request}",
        "",
    ]

    for worker in workers:
        wt_info = state.worktrees.get(worker, {})
        wt_path = wt_info.get("path", "")
        checkpoint = state.checkpoints.get(worker, "")
        agent_type = agent_types.get(worker, "?")

        lines.append(f"## {worker} ({agent_type})")
        lines.append("")
        lines.append(f"- 브랜치: `{state.worker_branches.get(worker, '(없음)')}`")
        lines.append(f"- 체크포인트: `{checkpoint or '(변경 없음)'}`")

        if checkpoint and wt_path:
            try:
                files = changed_files(state.base_commit, checkpoint, wt_path)
            except Exception:
                files = []
        else:
            files = []
        if files:
            lines.append(f"- 변경 파일 ({len(files)}개): " + ", ".join(files))
        else:
            lines.append("- 변경 파일: (없음)")

        checks = state.worker_tests.get(worker, [])
        if checks:
            failed = [c for c in checks if not c.get("success")]
            lines.append(
                f"- 체크: {len(checks) - len(failed)}/{len(checks)} 통과"
                + (f" ({len(failed)}개 실패)" if failed else "")
            )

        response = state.responses.get(worker, {})
        if response:
            dispositions = ", ".join(response.get("dispositions", [])) or "(없음)"
            lines.append(f"- 리뷰에 대한 응답: {dispositions}")

        lines.append("")

    lines.append("## 교차 리뷰 (Cross Review)")
    lines.append("")
    for review in state.cross_reviews.values():
        reviewer = review.get("reviewer", "?")
        target = review.get("target", "?")
        lines.append(f"### {reviewer} → {target}")
        lines.append("")
        lines.append(review.get("output_text", "").strip() or "(내용 없음)")
        lines.append("")

    lines.append("## 응답 전문 (Response)")
    lines.append("")
    for worker in workers:
        response = state.responses.get(worker)
        if not response:
            continue
        lines.append(f"### {worker}")
        lines.append("")
        lines.append(response.get("output_text", "").strip() or "(내용 없음)")
        lines.append("")

    lines.append("## 선택지")
    lines.append("")
    lines.append(
        "`SELECT_WORKER_1` / `SELECT_WORKER_2` / `SELECT_HYBRID` / `REWORK` / `CANCEL`"
    )

    return "\n".join(lines)


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
    # Both workers operate in their own worktree (disjoint files, disjoint
    # branches), so their agent calls launch concurrently — this is the
    # "PARALLEL" in PARALLEL COMPETITION. State mutation stays sequential and
    # happens only after both calls return, so there's no shared-state race:
    # each worker's coroutine only reads shared locals, never writes state.
    state.stage = Stage.INDEPENDENT_WORK.value
    for worker in workers:
        state.worker_status[worker] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    async def _independent_work(worker: str):
        wt_path = state.worktrees[worker]["path"]
        worker_spec = worker_specs[worker]
        prompt = build_independent_work_prompt(
            task_id=task_id,
            user_request=user_request,
            agent=worker,
            worktree_path=wt_path,
            base_commit=state.base_commit,
        )
        return await agent_runner.run_agent(
            agent_type=worker_spec.agent_type,
            prompt=prompt,
            cwd=wt_path,
            timeout_sec=agent_timeout_sec,
            model=worker_spec.model,
            label=f"worker_{worker_spec.agent_type}-independent_work",
        )

    independent_results = await asyncio.gather(
        *(_independent_work(worker) for worker in workers)
    )

    for worker, result in zip(workers, independent_results, strict=True):
        wt_info = state.worktrees[worker]
        wt_path = wt_info["path"]

        worker_spec = worker_specs[worker]
        agent_type = worker_spec.agent_type
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else worker_spec.model)
        )
        result.agent = agent_type
        result.agent_type = agent_type
        result.stage = Stage.INDEPENDENT_WORK.value
        result.model = effective_model
        result.requested_model = worker_spec.model
        result.effective_model = effective_model

        state.handoffs.append(
            {
                "stage": Stage.INDEPENDENT_WORK.value,
                "lane": worker,
                "agent": agent_type,
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
            agent=agent_type,
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

    # Both reviews read already-completed INDEPENDENT_WORK checkpoints and
    # target disjoint worktrees, so — same reasoning as INDEPENDENT_WORK —
    # the two agent calls launch concurrently; only result processing (and
    # its early-block-on-failure behavior) stays sequential, in pair order.
    pairs = [(workers[1], workers[0]), (workers[0], workers[1])]

    async def _cross_review(reviewer: str, target: str):
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

        return await agent_runner.run_agent(
            agent_type=reviewer_spec.agent_type,
            prompt=prompt,
            cwd=target_wt,
            timeout_sec=agent_timeout_sec,
            model=reviewer_spec.model,
            label=f"worker_{reviewer_spec.agent_type}-cross_review",
        )

    review_results = await asyncio.gather(
        *(_cross_review(reviewer, target) for reviewer, target in pairs)
    )

    for (reviewer, target), result in zip(pairs, review_results, strict=True):
        reviewer_spec = worker_specs[reviewer]
        reviewer_type = reviewer_spec.agent_type
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else reviewer_spec.model)
        )
        result.agent = reviewer_type
        result.agent_type = reviewer_type
        result.stage = Stage.CROSS_REVIEW.value
        result.model = effective_model
        result.requested_model = reviewer_spec.model
        result.effective_model = effective_model

        state.handoffs.append(
            {
                "stage": Stage.CROSS_REVIEW.value,
                "lane": reviewer,
                "agent": reviewer_type,
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

    # Each worker responds to a review of its own already-completed work, so
    # (same reasoning as the two stages above) both responses launch
    # concurrently; result processing stays sequential in worker order.
    response_pairs = [(workers[0], workers[1]), (workers[1], workers[0])]

    async def _response(worker: str, reviewer: str):
        review_key = f"{reviewer}_reviews_{worker}"
        review_text = state.cross_reviews.get(review_key, {}).get("output_text", "")

        worker_spec = worker_specs[worker]
        prompt = build_response_prompt(
            task_id=task_id,
            worker=worker,
            checkpoint=state.checkpoints.get(worker, ""),
            review_findings=review_text,
        )

        return await agent_runner.run_agent(
            agent_type=worker_spec.agent_type,
            prompt=prompt,
            cwd=state.worktrees[worker]["path"],
            timeout_sec=agent_timeout_sec,
            model=worker_spec.model,
            label=f"worker_{worker_spec.agent_type}-response",
        )

    response_results = await asyncio.gather(
        *(_response(worker, reviewer) for worker, reviewer in response_pairs)
    )

    for (worker, _reviewer), result in zip(
        response_pairs, response_results, strict=True
    ):
        worker_spec = worker_specs[worker]
        worker_type = worker_spec.agent_type
        effective_model = (
            result.effective_model
            if result.effective_model is not None
            else (result.model if result.model is not None else worker_spec.model)
        )
        result.agent = worker_type
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
                "agent": worker_type,
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

    # -- COMPARE --
    # Everything below is already in `state` from earlier stages; this only
    # renders it into one document a person can act on in under a minute,
    # instead of making them dig through the raw state.json / cross_reviews
    # dict. resume_mode_a later feeds this same text to the merge agent.
    state.stage = Stage.COMPARE.value
    compare_report = _build_compare_report(state)
    compare_report_path = state_mgr.write_output("COMPARE", "report", compare_report)
    state.compare_path = str(compare_report_path)
    state.stage_statuses[Stage.COMPARE.value] = StageStatus.DONE.value

    # -- WAITING_USER --
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

    On SELECT_WORKER_1/2/HYBRID, records the selected worker's worktree/branch
    as state.active_worktree/active_branch so a subsequent `run_mode_b` relay
    has something to continue (see TH-D15) — without this, MODE B's
    GIT_VERIFY stage always blocks with "No active worktree in state" for any
    task that started as MODE A, since only MODE C previously set these
    fields.
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

    # Record the chosen line of work as the "active" worktree/branch so a
    # later MODE B relay has something to continue on (see TH-D15). For
    # SELECT_HYBRID, MODE B can only continue one branch at a time, so the
    # first selected worker becomes primary — the merge prompt above already
    # told the integration agent to draw on both checkpoints regardless.
    if selected:
        primary_worker = selected[0]
        state.active_worktree = state.worktrees.get(primary_worker, {}).get("path")
        state.active_branch = state.worker_branches.get(primary_worker)
        # logical_agent/backend_agent: state.handoffs already recorded the
        # primary worker's agent_type during INDEPENDENT_WORK (see
        # _build_compare_report for the same lookup) — mode_a.py otherwise
        # never sets these, which left MODE B's relay prompt citing "unknown
        # agent" as the previous_agent for any task that started as MODE A.
        primary_agent_type = next(
            (
                h.get("agent_type")
                for h in state.handoffs
                if h.get("stage") == Stage.INDEPENDENT_WORK.value
                and h.get("lane") == primary_worker
            ),
            None,
        )
        state.logical_agent = primary_agent_type
        state.backend_agent = primary_agent_type

    # -- CODEX_MERGE --
    state.stage = Stage.CODEX_MERGE.value
    state.stage_statuses[Stage.CODEX_MERGE.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    # The COMPARE stage (run_mode_a) writes a human-readable comparison
    # report and records its path on state.compare_path. Feed that same
    # report to the merge agent so its integration choice is grounded in the
    # same evidence the human based their selection on. An older run_dir
    # from before compare_path existed (or a compare file since removed)
    # falls back to an empty string rather than failing the resume.
    compare_text = ""
    if state.compare_path:
        try:
            compare_text = Path(state.compare_path).read_text(encoding="utf-8")
        except OSError:
            compare_text = ""

    final_spec = proto_cfg.mode_a_final
    final_type = final_spec.agent_type
    prompt = build_merge_prompt(
        task_id=task_id,
        selection=selection,
        user_instruction=user_instruction,
        base_branch=state.base_branch,
        base_head=preflight.head,
        selected_details=selected_details,
        compare_text=compare_text,
    )

    result = await agent_runner.run_agent(
        agent_type=final_type,
        prompt=prompt,
        cwd=target_repo,
        timeout_sec=agent_timeout_sec,
        model=final_spec.model,
        label=f"{final_type}-merge",
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
            "agent_type": final_type,
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
