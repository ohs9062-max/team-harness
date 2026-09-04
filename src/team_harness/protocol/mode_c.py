"""MODE C — ROLE PIPELINE state machine.

Drives Claude DESIGN → Codex IMPLEMENT → System CHECK → Gemini(antigravity) REVIEW
on top of the existing team-harness Agent execution engine.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from typing import Any, Protocol as TypingProtocol

from team_harness.protocol.checks import CheckRunner, CheckResult
from team_harness.protocol.git import git_preflight
from team_harness.protocol.models import (
    AGENT_TYPE_MAP,
    AgentResult,
    CheckStatus,
    ProtocolState,
    ReviewVerdict,
    Stage,
    StageStatus,
    resolve_agent_type,
)
from team_harness.protocol.prompt import (
    build_design_prompt,
    build_fix_prompt,
    build_implement_prompt,
    build_review_prompt,
)
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worktree import (
    WorktreeRef,
    changed_files,
    checkpoint_worktree,
    create_worktree,
    diff_worktree,
)


class AgentRunner(TypingProtocol):
    """Interface for running an agent (real or mock).

    Implementations must return an ``AgentResult``.  The real implementation
    uses ``team_harness.agents.spawner.spawn()``.
    """

    async def run_agent(
        self,
        *,
        agent_type: str,
        prompt: str,
        cwd: str,
        timeout_sec: int,
    ) -> AgentResult: ...


def _extract_verdict(output: str) -> str:
    """Extract the VERDICT line from reviewer output."""
    for line in reversed(output.splitlines()):
        line = line.strip()
        match = re.match(r"VERDICT:\s*(PASS|FIX_REQUIRED|BLOCKED)", line, re.IGNORECASE)
        if match:
            return match.group(1).upper()
    return ReviewVerdict.BLOCKED.value


async def run_mode_c(
    *,
    task_id: str,
    user_request: str,
    target_repo: str,
    run_dir: str | Path,
    agent_runner: AgentRunner,
    max_review_cycles: int = 2,
    check_commands: list[list[str]] | None = None,
    auto_discover_checks: bool = True,
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
) -> ProtocolState:
    """Execute MODE C: ROLE PIPELINE.

    State machine:
        DEFINE → GIT_PREFLIGHT → WORKTREE_SETUP → DESIGN(claude)
        → IMPLEMENT(codex) → TEST(codex) → CHECK(system)
        → REVIEW(antigravity/agy)
        → PASS → FINAL
        → FIX_REQUIRED → FIX(codex) → TEST → CHECK → REVIEW (max cycles)
        → BLOCKED → terminate
    """
    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = ProtocolState(
        task_id=task_id,
        mode="C",
        user_request=user_request,
        target_repo=target_repo,
        max_review_cycles=max_review_cycles,
    )

    # -- DEFINE --
    state.stage = Stage.DEFINE.value
    state.status = StageStatus.RUNNING.value
    state.stage_statuses[Stage.DEFINE.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("stage.done", stage="DEFINE")

    # -- GIT_PREFLIGHT --
    state.stage = Stage.GIT_PREFLIGHT.value
    state.stage_statuses[Stage.GIT_PREFLIGHT.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    try:
        preflight = git_preflight(target_repo)
    except (ValueError, Exception) as exc:
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
    state_mgr.append_event(
        "stage.done",
        stage="GIT_PREFLIGHT",
        branch=preflight.branch,
        head=preflight.head,
    )

    # -- WORKTREE_SETUP --
    state.stage = Stage.WORKTREE_SETUP.value
    state.stage_statuses[Stage.WORKTREE_SETUP.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    try:
        wt = create_worktree(target_repo, task_id, "pipeline", state.base_commit)
    except Exception as exc:
        return _block(state, state_mgr, Stage.WORKTREE_SETUP.value, str(exc))

    state.worktrees["pipeline"] = {
        "label": wt.label,
        "branch": wt.branch,
        "path": wt.path,
        "base_commit": wt.base_commit,
    }
    state.active_worktree = wt.path
    state.active_branch = wt.branch
    state.stage_statuses[Stage.WORKTREE_SETUP.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event(
        "worktree.created",
        label="pipeline",
        path=wt.path,
        branch=wt.branch,
    )

    worktree_path = wt.path

    # -- DESIGN (Claude) --
    design_result = await _run_stage(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        stage=Stage.DESIGN.value,
        logical_agent="claude",
        prompt=build_design_prompt(
            task_id=task_id,
            user_request=user_request,
            worktree_path=worktree_path,
            base_commit=state.base_commit,
        ),
        cwd=worktree_path,
        timeout_sec=agent_timeout_sec,
    )
    if not design_result.success:
        return _block(
            state,
            state_mgr,
            Stage.DESIGN.value,
            design_result.error_message or "DESIGN stage failed",
        )

    # -- IMPLEMENT (Codex) --
    implement_result = await _run_stage(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        stage=Stage.IMPLEMENT.value,
        logical_agent="codex",
        prompt=build_implement_prompt(
            task_id=task_id,
            user_request=user_request,
            design_output=design_result.output_text,
            worktree_path=worktree_path,
            base_commit=state.base_commit,
        ),
        cwd=worktree_path,
        timeout_sec=agent_timeout_sec,
    )
    if not implement_result.success:
        return _block(
            state,
            state_mgr,
            Stage.IMPLEMENT.value,
            implement_result.error_message or "IMPLEMENT stage failed",
        )

    # -- Review loop (CHECK → REVIEW, with FIX cycles) --
    review_cycle = 0
    while review_cycle <= max_review_cycles:
        state.review_cycle = review_cycle

        # -- CHECK (System) --
        check_status = _run_checks(
            state=state,
            state_mgr=state_mgr,
            worktree_path=worktree_path,
            check_commands=check_commands,
            auto_discover=auto_discover_checks,
            timeout_sec=check_timeout_sec,
        )
        if check_status == CheckStatus.FAIL.value:
            return _block(
                state, state_mgr, Stage.CHECK.value, "Deterministic checks failed"
            )

        # Get diff for review
        try:
            current_diff = diff_worktree(
                state.base_commit,
                _get_head(worktree_path),
                worktree_path,
            )
        except Exception:
            current_diff = ""

        # -- REVIEW (Gemini → antigravity/agy) --
        review_result = await _run_stage(
            state=state,
            state_mgr=state_mgr,
            agent_runner=agent_runner,
            stage=Stage.REVIEW.value,
            logical_agent="gemini",
            prompt=build_review_prompt(
                task_id=task_id,
                user_request=user_request,
                design_output=design_result.output_text,
                diff_text=current_diff,
                check_results=_format_checks(state.checks),
                worktree_path=worktree_path,
                base_commit=state.base_commit,
                review_cycle=review_cycle,
            ),
            cwd=worktree_path,
            timeout_sec=agent_timeout_sec,
        )

        verdict = _extract_verdict(review_result.output_text)
        state.review_verdict = verdict
        state_mgr.append_event(
            "review.verdict",
            verdict=verdict,
            cycle=review_cycle,
        )

        if verdict == ReviewVerdict.PASS.value:
            break
        elif verdict == ReviewVerdict.BLOCKED.value:
            return _block(
                state, state_mgr, Stage.REVIEW.value, "Reviewer returned BLOCKED"
            )
        elif verdict == ReviewVerdict.FIX_REQUIRED.value:
            review_cycle += 1
            if review_cycle > max_review_cycles:
                return _block(
                    state,
                    state_mgr,
                    Stage.REVIEW.value,
                    f"Max review cycles ({max_review_cycles}) exceeded",
                )

            # -- FIX (Codex) --
            fix_result = await _run_stage(
                state=state,
                state_mgr=state_mgr,
                agent_runner=agent_runner,
                stage=Stage.FIX.value,
                logical_agent="codex",
                prompt=build_fix_prompt(
                    task_id=task_id,
                    user_request=user_request,
                    review_findings=review_result.output_text,
                    worktree_path=worktree_path,
                    base_commit=state.base_commit,
                    review_cycle=review_cycle,
                ),
                cwd=worktree_path,
                timeout_sec=agent_timeout_sec,
            )
            if not fix_result.success:
                return _block(
                    state,
                    state_mgr,
                    Stage.FIX.value,
                    fix_result.error_message or "FIX stage failed",
                )
        else:
            return _block(
                state,
                state_mgr,
                Stage.REVIEW.value,
                f"Unknown review verdict: {verdict}",
            )

    # -- Checkpoint --
    try:
        cp = checkpoint_worktree(worktree_path, task_id, "pipeline")
        state.checkpoints["pipeline"] = cp
        state_mgr.append_event("checkpoint.created", label="pipeline", commit=cp)
    except RuntimeError:
        # No changes to checkpoint (e.g. mock agents didn't write files)
        pass

    # -- FINAL --
    state.stage = Stage.FINAL.value
    state.stage_statuses[Stage.FINAL.value] = StageStatus.DONE.value
    state.status = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("run.finished", status="DONE")

    return state


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _run_stage(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    agent_runner: AgentRunner,
    stage: str,
    logical_agent: str,
    prompt: str,
    cwd: str,
    timeout_sec: int,
) -> AgentResult:
    """Run a single agent stage and record the result."""
    agent_type = resolve_agent_type(logical_agent)
    state.stage = stage
    state.logical_agent = logical_agent
    state.backend_agent = agent_type
    state.stage_statuses[stage] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    state_mgr.append_event(
        "stage.started",
        stage=stage,
        logical_agent=logical_agent,
        backend_agent=agent_type,
    )

    result = await agent_runner.run_agent(
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        timeout_sec=timeout_sec,
    )
    result.agent = logical_agent
    result.agent_type = agent_type
    result.stage = stage

    status = StageStatus.DONE.value if result.success else StageStatus.FAILED.value
    state.stage_statuses[stage] = status
    state.handoffs.append({
        "stage": stage,
        "agent": logical_agent,
        "agent_type": agent_type,
        "success": result.success,
        "exit_code": result.exit_code,
        "review_verdict": result.review_verdict,
    })
    state_mgr.save_state(state)
    state_mgr.append_event(
        "stage.finished",
        stage=stage,
        agent=logical_agent,
        success=result.success,
    )

    # Save output
    if result.output_text:
        state_mgr.write_output(stage, logical_agent, result.output_text)

    return result


def _run_checks(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    worktree_path: str,
    check_commands: list[list[str]] | None,
    auto_discover: bool,
    timeout_sec: int,
) -> str:
    """Run deterministic checks and return the status string."""
    state.stage = Stage.CHECK.value
    state.stage_statuses[Stage.CHECK.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)

    runner = CheckRunner(worktree_path, timeout_sec=timeout_sec)
    commands: list[list[str]] = list(check_commands or [])
    if auto_discover:
        commands.extend(runner.discover())

    # Deduplicate
    unique: list[list[str]] = []
    for cmd in commands:
        if cmd not in unique:
            unique.append(cmd)

    if not unique:
        state.test_status = CheckStatus.WAIVED.value
        state.stage_statuses[Stage.CHECK.value] = StageStatus.DONE.value
        state_mgr.save_state(state)
        state_mgr.append_event("check.waived", reason="no check commands found")
        return CheckStatus.WAIVED.value

    results = runner.run_all(unique)
    state.checks = [r.to_dict() for r in results]

    all_pass = all(r.success for r in results)
    status = CheckStatus.PASS.value if all_pass else CheckStatus.FAIL.value
    state.test_status = status
    state.stage_statuses[Stage.CHECK.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("check.finished", status=status, count=len(results))
    return status


def _format_checks(checks: list[dict[str, Any]]) -> str:
    """Format check results for inclusion in a prompt."""
    if not checks:
        return "No deterministic checks were executed (WAIVED)."
    lines = []
    for c in checks:
        cmd = " ".join(c.get("command", []))
        ok = "PASS" if c.get("success") else "FAIL"
        lines.append(f"- [{ok}] {cmd}")
        stderr = c.get("stderr", "")
        if stderr and not c.get("success"):
            lines.append(f"  stderr: {stderr[:500]}")
    return "\n".join(lines)


def _get_head(worktree_path: str) -> str:
    """Get the current HEAD commit of a worktree."""
    import subprocess

    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _block(
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    stage: str,
    reason: str,
) -> ProtocolState:
    """Move the state to BLOCKED and persist."""
    state.stage = stage
    state.status = StageStatus.BLOCKED.value
    state.stage_statuses[stage] = StageStatus.BLOCKED.value
    state.blocker = reason
    state_mgr.save_state(state)
    state_mgr.append_event("run.blocked", stage=stage, reason=reason)
    return state
