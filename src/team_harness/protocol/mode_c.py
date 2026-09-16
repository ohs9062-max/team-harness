"""MODE C — ROLE PIPELINE state machine.

Drives DESIGN → IMPLEMENT → TEST → System CHECK → REVIEW
(defaults to Claude DESIGN → Codex IMPLEMENT → Gemini/antigravity REVIEW)
on top of the existing team-harness Agent execution engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import UTC
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any
from typing import Protocol as TypingProtocol
import uuid

from team_harness.agents import spawner
from team_harness.agents.api_error_classifier import classify_agent_failure
from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.agents.process_identity import signal_group
from team_harness.agents.rate_limits import detect_rate_limit_from_path
from team_harness.agents.rate_limits import RateLimitCircuitBreaker
from team_harness.agents.rate_limits import RateLimitTrip
from team_harness.agents.registry import resolve_template
from team_harness.agents.tmux_view import tmux_available
from team_harness.agents.tmux_view import TmuxViewer
from team_harness.config import Config
from team_harness.protocol.checks import CheckRunner
from team_harness.protocol.checks import evaluate_checks
from team_harness.protocol.config import load_protocol_config
from team_harness.protocol.config import ProtocolAgentSpec
from team_harness.protocol.config import ProtocolConfig
from team_harness.protocol.git import git_preflight
from team_harness.protocol.models import AgentResult
from team_harness.protocol.models import CheckStatus
from team_harness.protocol.models import ProtocolState
from team_harness.protocol.models import ReviewVerdict
from team_harness.protocol.models import Stage
from team_harness.protocol.models import StageStatus
from team_harness.protocol.prompt import build_design_prompt
from team_harness.protocol.prompt import build_fix_prompt
from team_harness.protocol.prompt import build_implement_prompt
from team_harness.protocol.prompt import build_review_prompt
from team_harness.protocol.state import ProtocolStateManager
from team_harness.protocol.worker_output import extract_final_text
from team_harness.protocol.worktree import changed_files
from team_harness.protocol.worktree import checkpoint_worktree
from team_harness.protocol.worktree import create_worktree
from team_harness.protocol.worktree import diff_worktree


class AgentRunner(TypingProtocol):
    """Interface for running an agent (real or mock).

    Implementations must return an ``AgentResult``. The real implementation
    delegates to ``team_harness.agents.spawner.spawn()``.
    """

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
    ) -> AgentResult: ...


class TeamHarnessAgentRunner:
    """AgentRunner implementation that delegates to team_harness execution engine."""

    def __init__(
        self,
        *,
        config: Config | None = None,
        manager: AgentManager | None = None,
        log_dir: str | Path | None = None,
        tmux_session: str | None = None,
        verbose: bool = True,
        breaker: RateLimitCircuitBreaker | None = None,
    ) -> None:
        self.config = config or Config()
        self.manager = manager or AgentManager()
        # Run-scoped rate-limit circuit (TH-D18). A protocol run fans out
        # several workers over the same few agent families — MODE A alone
        # spawns per worker for INDEPENDENT_WORK, CROSS_REVIEW and RESPONSE.
        # Once a family has returned a hard provider 429 every later spawn on
        # that family is doomed, so we refuse to launch it and say why instead
        # of paying the full startup-plus-timeout cost again. The role's
        # configured backend is never silently swapped for another one (TH-D6).
        self.breaker = breaker or RateLimitCircuitBreaker(
            enabled=self.config.rate_limit_circuit_breaker,
            default_cooldown_s=self.config.rate_limit_default_cooldown_s,
        )
        # Print one line per worker completion (agent + effective model/effort)
        # so a person watching `th protocol` output always knows who actually
        # did each piece of work, not just the final stage/status summary.
        # Set False for library/SDK use where stdout output is unwanted.
        self.verbose = verbose
        self.log_dir = (
            Path(log_dir).resolve()
            if log_dir
            else Path(self.config.output_dir).resolve()
        )
        self.log_dir.mkdir(parents=True, exist_ok=True)
        # Optional live view (TH-D12): when set, every spawned worker also
        # gets a tmux window tailing its stdout log. Purely observational —
        # never wired to the worker's stdin, never required for correctness.
        # A caller that asks for it but has no `tmux` on PATH silently gets
        # no window rather than a broken run.
        self.tmux_viewer = (
            TmuxViewer(tmux_session)
            if tmux_session is not None and tmux_available()
            else None
        )

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
        resolve_template(agent_type=agent_type, config=self.config)

        trip = self.breaker.active_trip(agent_type)
        if trip is not None:
            return self._rate_limited_result(
                agent_type=agent_type, requested_model=model, label=label, trip=trip
            )

        started = time.monotonic()
        agent_id = f"{agent_type}_{uuid.uuid4().hex[:8]}"
        stdout_path = self.log_dir / f"{agent_id}_stdout.log"
        stderr_path = self.log_dir / f"{agent_id}_stderr.log"

        spawn_result = await spawner.spawn(
            agent_id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            cwd=Path(cwd),
            config=self.config,
            log_dir=self.log_dir,
            model=model,
            effort=effort,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )

        agent_state = AgentState(
            id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            cwd=cwd,
            proc=spawn_result.proc,
            spawn_time=datetime.now(UTC),
            stdout_log=stdout_path,
            stderr_log=stderr_path,
            pgid=spawn_result.pgid,
            effective_model=spawn_result.effective_model,
        )
        self.manager.register(agent_state)
        if self.tmux_viewer is not None:
            await self.tmux_viewer.open_log_window(
                window_name=label or agent_id, log_path=stdout_path
            )

        timed_out = False
        try:
            await asyncio.wait_for(spawn_result.proc.wait(), timeout=timeout_sec)
        except TimeoutError:
            timed_out = True
            if spawn_result.pgid:
                signal_group(spawn_result.pgid, signal.SIGKILL)
            elif spawn_result.proc.returncode is None:
                spawn_result.proc.kill()
            await spawn_result.proc.wait()

        duration = time.monotonic() - started
        returncode = spawn_result.proc.returncode or 0

        stdout_text = (
            stdout_path.read_text(encoding="utf-8", errors="replace")
            if stdout_path.exists()
            else ""
        )
        stderr_text = (
            stderr_path.read_text(encoding="utf-8", errors="replace")
            if stderr_path.exists()
            else ""
        )

        success = (returncode == 0) and not timed_out
        error_msg: str | None = None
        if timed_out:
            error_msg = f"Agent timed out after {timeout_sec}s"
        elif returncode != 0:
            suffix = f": {stderr_text[:500]}" if stderr_text else ""
            error_msg = f"Agent process exited with code {returncode}{suffix}"

        classification: dict[str, Any] | None = None
        if not success:
            classification = self._classify_failure(
                agent_type=agent_type,
                effective_model=spawn_result.effective_model,
                stdout_path=stdout_path,
                stdout_text=stdout_text,
                stderr_text=stderr_text,
                returncode=returncode,
            )
        if classification is not None:
            error_msg = (
                f"{error_msg} [{classification['category']}: "
                f"{classification['detail']}]"
            )

        if self.verbose:
            print(
                _completion_line(
                    label=label or agent_id,
                    agent_type=agent_type,
                    effective_model=spawn_result.effective_model,
                    effort=effort,
                    success=success,
                )
            )

        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            model=spawn_result.effective_model,
            requested_model=model,
            effective_model=spawn_result.effective_model,
            stage="",
            success=success,
            exit_code=returncode,
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
            output_text=extract_final_text(stdout_text),
            duration_sec=duration,
            error_message=error_msg,
            failure_classification=classification,
        )

    def _classify_failure(
        self,
        *,
        agent_type: str,
        effective_model: str | None,
        stdout_path: Path,
        stdout_text: str,
        stderr_text: str,
        returncode: int,
    ) -> dict[str, Any] | None:
        """Explain a failed stage, and open the family circuit on a hard 429.

        Two layers, matching the coordinator path. First the strict JSONL scan
        of TH-D10, which only fires on an explicit provider rejection and is
        what may trip the circuit. Otherwise a best-effort regex scan of the
        worker's own output, which is advisory only: it labels the failure for
        the human reading ``protocol_state.json`` but never blocks a family,
        because a test log that merely mentions "rate limit" is not evidence
        that this account is actually throttled.
        """

        if returncode != 0:
            try:
                signal_ = detect_rate_limit_from_path(stdout_path)
            except (OSError, UnicodeError):
                signal_ = None
            if signal_ is not None:
                trip = self.breaker.trip(
                    family=agent_type, model=effective_model, signal=signal_
                )
                if trip is not None:
                    return {
                        "is_api_error": True,
                        "category": "rate_limit",
                        "detail": trip.reason,
                        "family": trip.family,
                        "model": trip.model,
                        "resets_at": trip.resets_at.isoformat(),
                        "suggested_action": (
                            f"Agent family {trip.family!r} is rate limited until "
                            f"{trip.resets_at.isoformat()}. Re-run this stage after "
                            "that, or configure the role onto another family."
                        ),
                    }

        advisory = classify_agent_failure(stderr_text, stdout_text)
        if advisory is None:
            return None
        return {
            "is_api_error": advisory.is_api_error,
            "category": advisory.category,
            "detail": advisory.detail,
        }

    def _rate_limited_result(
        self,
        *,
        agent_type: str,
        requested_model: str | None,
        label: str | None,
        trip: RateLimitTrip,
    ) -> AgentResult:
        """Refuse to launch a stage whose family is inside an open circuit."""

        reason = (
            f"Agent family {trip.family!r} is rate limited until "
            f"{trip.resets_at.isoformat()} ({trip.reason}); worker not launched"
        )
        if self.verbose:
            print(f"[protocol] {label or agent_type} 건너뜀 — {reason}")
        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            stage="",
            success=False,
            requested_model=requested_model,
            spawned=False,
            error_message=reason,
            failure_classification={
                "is_api_error": True,
                "category": "rate_limit",
                "detail": trip.reason,
                "family": trip.family,
                "model": trip.model,
                "resets_at": trip.resets_at.isoformat(),
                "suggested_action": (
                    f"Agent family {trip.family!r} is rate limited until "
                    f"{trip.resets_at.isoformat()}. Re-run this stage after "
                    "that, or configure the role onto another family."
                ),
            },
        )


def _short_model_label(model: str | None, effort: str | None) -> str:
    """Compact model+effort label, e.g. "gpt-5.6-terra"/"high" -> "terra-high".

    Only strips the "gpt-5.6-" prefix (codex's naming scheme, where the
    tier name alone is unambiguous); other agents' model strings (claude,
    antigravity) are shown as-is since they aren't in that family.
    """

    if model is None:
        return "default"
    short = model.removeprefix("gpt-5.6-")
    return f"{short}-{effort}" if effort else short


def _completion_line(
    *,
    label: str,
    agent_type: str,
    effective_model: str | None,
    effort: str | None,
    success: bool,
) -> str:
    status_word = "완료" if success else "실패"
    model_label = _short_model_label(effective_model, effort)
    return f"[protocol] {label} {status_word} — {agent_type} {model_label}"


def _extract_verdict(output: str) -> str | None:
    """Extract the VERDICT line from reviewer output.

    Must return exactly PASS, FIX_REQUIRED, or BLOCKED.
    Returns None if no unambiguous verdict is found.
    """
    if not output:
        return None
    for line in reversed(output.splitlines()):
        line = line.strip()
        match = re.match(
            r"^VERDICT:\s*(PASS|FIX_REQUIRED|BLOCKED)\b", line, re.IGNORECASE
        )
        if match:
            return match.group(1).upper()
    return None


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
    worktrees_base_dir: str | Path | None = None,
    protocol_config: ProtocolConfig | None = None,
) -> ProtocolState:
    """Execute MODE C: ROLE PIPELINE.

    State machine:
        DEFINE → GIT_PREFLIGHT → WORKTREE_SETUP → DESIGN(claude)
        → IMPLEMENT(codex) → TEST(codex) → CHECK(system)
        → REVIEW(antigravity/agy)
        → PASS → CHECKPOINT → FINAL
        → FIX_REQUIRED → FIX(codex) → TEST → CHECK → REVIEW (max cycles)
        → BLOCKED → terminate
    """
    proto_cfg = protocol_config or load_protocol_config()
    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = ProtocolState(
        task_id=task_id,
        mode="C",
        user_request=user_request,
        target_repo=target_repo,
        max_review_cycles=max_review_cycles,
        run_id=run_path.name,
    )

    # -- DEFINE --
    state.stage = Stage.DEFINE.value
    state.status = StageStatus.RUNNING.value
    state.stage_statuses[Stage.DEFINE.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("DEFINE", stage=Stage.DEFINE.value)

    # -- GIT_PREFLIGHT --
    state.stage = Stage.GIT_PREFLIGHT.value
    state.stage_statuses[Stage.GIT_PREFLIGHT.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    try:
        preflight = git_preflight(target_repo)
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        return _block(
            state, state_mgr, Stage.GIT_PREFLIGHT.value, f"Git preflight failed: {exc}"
        )

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
        "GIT_PREFLIGHT", branch=preflight.branch, head=preflight.head
    )

    # -- WORKTREE_SETUP --
    state.stage = Stage.WORKTREE_SETUP.value
    state.stage_statuses[Stage.WORKTREE_SETUP.value] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    try:
        wt = create_worktree(
            target_repo,
            task_id,
            "pipeline",
            state.base_commit,
            worktrees_base_dir=worktrees_base_dir,
        )
    except (OSError, subprocess.CalledProcessError, ValueError, RuntimeError) as exc:
        return _block(
            state,
            state_mgr,
            Stage.WORKTREE_SETUP.value,
            f"Worktree creation failed: {exc}",
        )

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
        "WORKTREE_CREATED", label="pipeline", path=wt.path, branch=wt.branch
    )

    worktree_path = wt.path

    return await _run_pipeline(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        proto_cfg=proto_cfg,
        task_id=task_id,
        user_request=user_request,
        worktree_path=worktree_path,
        start_stage=Stage.DESIGN.value,
        design_output="",
        max_review_cycles=max_review_cycles,
        check_commands=check_commands,
        auto_discover_checks=auto_discover_checks,
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
    )


RESUMABLE_MODE_C_STAGES: tuple[str, ...] = (
    Stage.DESIGN.value,
    Stage.IMPLEMENT.value,
    Stage.REVIEW.value,
)


async def resume_mode_c(
    *,
    run_dir: str | Path,
    agent_runner: AgentRunner,
    from_stage: str | None = None,
    check_commands: list[list[str]] | None = None,
    auto_discover_checks: bool = True,
    agent_timeout_sec: int = 600,
    check_timeout_sec: int = 300,
    protocol_config: ProtocolConfig | None = None,
) -> ProtocolState:
    """Re-enter a saved MODE C run at *from_stage* instead of from the start.

    A pipeline that blocks at REVIEW has already paid for DESIGN and IMPLEMENT;
    re-running it from DEFINE spends those worker turns again for no new
    information. This reuses the persisted state, the frozen base commit and
    the existing task worktree, so only the stages from *from_stage* onward run.

    *from_stage* defaults to the earliest stage that is not DONE, which after a
    block is the stage that blocked. Resuming is only meaningful at a stage that
    consumes durable inputs — DESIGN (nothing prior), IMPLEMENT (the recorded
    design) or REVIEW (the worktree's current diff). FIX is reached by resuming
    at REVIEW, which re-reads the code as it now stands rather than replaying a
    stale set of findings.

    The base working tree is still never touched (TH-D16), and nothing is
    re-checked out: if the worktree is gone the run is BLOCKED with that reason
    rather than silently recreated, since its uncommitted work cannot be
    reconstructed (TH-D11).
    """

    run_path = Path(run_dir).resolve()
    state_mgr = ProtocolStateManager(run_path)
    state = state_mgr.load_state()
    if state is None:
        raise ValueError(f"No protocol state found in {run_path}")
    if state.mode != "C":
        raise ValueError(
            f"Run {state.task_id} is MODE {state.mode}, not MODE C. "
            "MODE A runs are continued with resume_mode_a."
        )

    stage = from_stage or _first_unfinished_stage(state)
    if stage not in RESUMABLE_MODE_C_STAGES:
        raise ValueError(
            f"Cannot resume MODE C at {stage!r}. "
            f"Resumable stages: {list(RESUMABLE_MODE_C_STAGES)}"
        )

    pipeline = state.worktrees.get("pipeline")
    worktree_path = (pipeline or {}).get("path", "")
    if not worktree_path or not Path(worktree_path).is_dir():
        return _block(
            state,
            state_mgr,
            stage,
            f"Task worktree is missing ({worktree_path or 'not recorded'}); "
            "cannot resume. Start a fresh run instead.",
        )

    design_output = ""
    if stage != Stage.DESIGN.value:
        design_output = _read_stage_output(state_mgr, "design")
        if not design_output.strip():
            return _block(
                state,
                state_mgr,
                stage,
                "No recorded DESIGN output to resume from; "
                f"resume at {Stage.DESIGN.value} instead.",
            )

    proto_cfg = protocol_config or load_protocol_config()
    state.status = StageStatus.RUNNING.value
    state.blocker = None
    state_mgr.append_event("RESUMED", stage=stage, mode="C")
    state_mgr.save_state(state)

    return await _run_pipeline(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        proto_cfg=proto_cfg,
        task_id=state.task_id,
        user_request=state.user_request,
        worktree_path=worktree_path,
        start_stage=stage,
        design_output=design_output,
        max_review_cycles=state.max_review_cycles,
        check_commands=check_commands,
        auto_discover_checks=auto_discover_checks,
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
    )


# The order stages first become reachable, used to pick a default resume point.
_MODE_C_STAGE_ORDER: tuple[str, ...] = (
    Stage.DEFINE.value,
    Stage.GIT_PREFLIGHT.value,
    Stage.WORKTREE_SETUP.value,
    Stage.DESIGN.value,
    Stage.IMPLEMENT.value,
    Stage.TEST.value,
    Stage.CHECK.value,
    Stage.REVIEW.value,
    Stage.FIX.value,
    Stage.FINAL.value,
)


def _first_unfinished_stage(state: ProtocolState) -> str:
    """The earliest pipeline stage that did not finish, mapped to a resume point.

    A stage that blocked mid-loop (CHECK, FIX, TEST) is not itself a resume
    point: re-entering at REVIEW re-reads the worktree as it now stands, which
    is the only honest way to continue from a half-finished fix cycle.
    """
    for candidate in _MODE_C_STAGE_ORDER:
        if state.stage_statuses.get(candidate) != StageStatus.DONE.value:
            if candidate in RESUMABLE_MODE_C_STAGES:
                return candidate
            if candidate in (
                Stage.DEFINE.value,
                Stage.GIT_PREFLIGHT.value,
                Stage.WORKTREE_SETUP.value,
            ):
                return Stage.DESIGN.value
            return Stage.REVIEW.value
    return Stage.REVIEW.value


def _read_stage_output(state_mgr: ProtocolStateManager, stage_key: str) -> str:
    """Read back whatever a stage wrote under protocol_outputs/<stage_key>/."""
    directory = state_mgr.run_dir / "protocol_outputs" / stage_key
    if not directory.is_dir():
        return ""
    for path in sorted(directory.glob("*.md")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if text.strip():
            return text
    return ""


async def _run_pipeline(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    agent_runner: AgentRunner,
    proto_cfg: ProtocolConfig,
    task_id: str,
    user_request: str,
    worktree_path: str,
    start_stage: str,
    design_output: str,
    max_review_cycles: int,
    check_commands: list[list[str]] | None,
    auto_discover_checks: bool,
    agent_timeout_sec: int,
    check_timeout_sec: int,
) -> ProtocolState:
    """Run MODE C from *start_stage* onward over an already-prepared worktree.

    Shared by `run_mode_c` (which starts at DESIGN after building the worktree)
    and `resume_mode_c` (which starts later over the worktree that already
    exists), so there is exactly one implementation of the pipeline.
    """

    run_design = start_stage == Stage.DESIGN.value
    run_implement = start_stage in (Stage.DESIGN.value, Stage.IMPLEMENT.value)

    # -- DESIGN --
    design_spec = proto_cfg.mode_c_design
    if run_design:
        designed = await _pipeline_design(
            state=state,
            state_mgr=state_mgr,
            agent_runner=agent_runner,
            design_spec=design_spec,
            task_id=task_id,
            user_request=user_request,
            worktree_path=worktree_path,
            agent_timeout_sec=agent_timeout_sec,
        )
        if designed is None:
            return state
        design_output = designed

    if run_implement:
        implemented = await _pipeline_implement(
            state=state,
            state_mgr=state_mgr,
            agent_runner=agent_runner,
            implement_spec=proto_cfg.mode_c_implement,
            task_id=task_id,
            user_request=user_request,
            design_output=design_output,
            worktree_path=worktree_path,
            agent_timeout_sec=agent_timeout_sec,
        )
        if not implemented:
            return state

    return await _pipeline_review_loop(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        proto_cfg=proto_cfg,
        task_id=task_id,
        user_request=user_request,
        worktree_path=worktree_path,
        design_output=design_output,
        max_review_cycles=max_review_cycles,
        check_commands=check_commands,
        auto_discover_checks=auto_discover_checks,
        agent_timeout_sec=agent_timeout_sec,
        check_timeout_sec=check_timeout_sec,
    )


async def _pipeline_design(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    agent_runner: AgentRunner,
    design_spec: ProtocolAgentSpec,
    task_id: str,
    user_request: str,
    worktree_path: str,
    agent_timeout_sec: int,
) -> str | None:
    """Run DESIGN. Returns its output text, or None when the run is blocked."""
    state_mgr.append_event(
        "DESIGN_STARTED",
        agent=design_spec.agent_type,
        role="DESIGN",
        model=design_spec.model,
        requested_model=design_spec.model,
    )
    design_result = await _run_stage(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        stage=Stage.DESIGN.value,
        role="DESIGN",
        spec=design_spec,
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
        _block(
            state,
            state_mgr,
            Stage.DESIGN.value,
            design_result.error_message or "DESIGN stage failed",
        )
        return None
    if not design_result.output_text or not design_result.output_text.strip():
        _block(
            state,
            state_mgr,
            Stage.DESIGN.value,
            f"{design_spec.agent_type} produced no design output",
        )
        return None

    state_mgr.write_output("design", design_spec.agent_type, design_result.output_text)
    state_mgr.append_event(
        "DESIGN_DONE",
        agent=design_spec.agent_type,
        role="DESIGN",
        model=design_result.model,
        requested_model=design_result.requested_model,
        effective_model=design_result.effective_model,
    )
    return design_result.output_text


async def _pipeline_implement(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    agent_runner: AgentRunner,
    implement_spec: ProtocolAgentSpec,
    task_id: str,
    user_request: str,
    design_output: str,
    worktree_path: str,
    agent_timeout_sec: int,
) -> bool:
    """Run IMPLEMENT and the self-reported TEST. False when the run is blocked."""
    state_mgr.append_event(
        "IMPLEMENT_STARTED",
        agent=implement_spec.agent_type,
        role="IMPLEMENT",
        model=implement_spec.model,
        requested_model=implement_spec.model,
    )
    implement_result = await _run_stage(
        state=state,
        state_mgr=state_mgr,
        agent_runner=agent_runner,
        stage=Stage.IMPLEMENT.value,
        role="IMPLEMENT",
        spec=implement_spec,
        prompt=build_implement_prompt(
            task_id=task_id,
            user_request=user_request,
            design_output=design_output,
            worktree_path=worktree_path,
            base_commit=state.base_commit,
        ),
        cwd=worktree_path,
        timeout_sec=agent_timeout_sec,
    )
    if not implement_result.success:
        _block(
            state,
            state_mgr,
            Stage.IMPLEMENT.value,
            implement_result.error_message or "IMPLEMENT stage failed",
        )
        return False

    state_mgr.write_output(
        "implement", implement_spec.agent_type, implement_result.output_text
    )
    state_mgr.append_event(
        "IMPLEMENT_DONE",
        agent=implement_spec.agent_type,
        role="IMPLEMENT",
        model=implement_result.model,
        requested_model=implement_result.requested_model,
        effective_model=implement_result.effective_model,
    )

    # Record changed files from implementation
    try:
        current_head = _get_head(worktree_path)
        current_changed = changed_files(state.base_commit, current_head, worktree_path)
        state.changed_files = current_changed
    except (OSError, subprocess.CalledProcessError):
        state.changed_files = []

    # -- TEST (self-report) --
    state.stage = Stage.TEST.value
    state.stage_statuses[Stage.TEST.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event(
        "TEST_DONE", agent=implement_spec.agent_type, role="IMPLEMENT"
    )
    return True


async def _pipeline_review_loop(
    *,
    state: ProtocolState,
    state_mgr: ProtocolStateManager,
    agent_runner: AgentRunner,
    proto_cfg: ProtocolConfig,
    task_id: str,
    user_request: str,
    worktree_path: str,
    design_output: str,
    max_review_cycles: int,
    check_commands: list[list[str]] | None,
    auto_discover_checks: bool,
    agent_timeout_sec: int,
    check_timeout_sec: int,
) -> ProtocolState:
    """CHECK → REVIEW with FIX cycles, then the checkpoint and FINAL."""

    # -- Review loop (CHECK → REVIEW, with FIX cycles) --
    review_cycle = 0
    review_spec = proto_cfg.mode_c_review
    while True:
        state.review_cycle = review_cycle

        # -- CHECK (System deterministic) --
        check_status = _run_checks(
            state=state,
            state_mgr=state_mgr,
            worktree_path=worktree_path,
            check_commands=check_commands,
            auto_discover=auto_discover_checks,
            timeout_sec=check_timeout_sec,
        )

        # Get diff for review
        try:
            current_head = _get_head(worktree_path)
            current_diff = diff_worktree(state.base_commit, current_head, worktree_path)
        except (OSError, subprocess.CalledProcessError):
            current_diff = ""

        # -- REVIEW --
        state_mgr.append_event(
            "REVIEW_STARTED",
            cycle=review_cycle,
            agent=review_spec.agent_type,
            role="REVIEW",
            model=review_spec.model,
            requested_model=review_spec.model,
        )
        review_result = await _run_stage(
            state=state,
            state_mgr=state_mgr,
            agent_runner=agent_runner,
            stage=Stage.REVIEW.value,
            role="REVIEW",
            spec=review_spec,
            prompt=build_review_prompt(
                task_id=task_id,
                user_request=user_request,
                design_output=design_output,
                diff_text=current_diff,
                check_results=_format_checks(state.checks),
                worktree_path=worktree_path,
                base_commit=state.base_commit,
                review_cycle=review_cycle,
            ),
            cwd=worktree_path,
            timeout_sec=agent_timeout_sec,
        )

        if not review_result.success:
            return _block(
                state,
                state_mgr,
                Stage.REVIEW.value,
                review_result.error_message or "REVIEW stage execution failed",
            )

        verdict = _extract_verdict(review_result.output_text)
        if verdict is None:
            return _block(
                state,
                state_mgr,
                Stage.REVIEW.value,
                "Reviewer produced unclear verdict (missing VERDICT: PASS|FIX_REQUIRED|BLOCKED)",
            )

        state.review_verdict = verdict
        state_mgr.write_output(
            f"review_cycle_{review_cycle}",
            review_spec.agent_type,
            review_result.output_text,
        )
        state_mgr.append_event(
            "REVIEW_RESULT",
            verdict=verdict,
            cycle=review_cycle,
            agent=review_spec.agent_type,
            role="REVIEW",
            model=review_result.model,
            requested_model=review_result.requested_model,
            effective_model=review_result.effective_model,
        )

        if verdict == ReviewVerdict.BLOCKED.value:
            return _block(
                state, state_mgr, Stage.REVIEW.value, "Reviewer returned BLOCKED"
            )

        # CHECK FAIL인데 agy PASS인 경우 → FINAL 금지
        if (
            verdict == ReviewVerdict.PASS.value
            and check_status == CheckStatus.FAIL.value
        ):
            if review_cycle < max_review_cycles:
                # FIX loop 기회 부여
                verdict = ReviewVerdict.FIX_REQUIRED.value
            else:
                return _block(
                    state,
                    state_mgr,
                    Stage.CHECK.value,
                    "System CHECK failed despite REVIEW PASS (cannot proceed to FINAL)",
                )

        if verdict == ReviewVerdict.PASS.value:
            break

        if verdict == ReviewVerdict.FIX_REQUIRED.value:
            review_cycle += 1
            if review_cycle > max_review_cycles:
                return _block(
                    state,
                    state_mgr,
                    Stage.REVIEW.value,
                    f"Max review cycles ({max_review_cycles}) exceeded",
                )

            # -- FIX --
            fix_spec = proto_cfg.mode_c_implement
            state_mgr.append_event(
                "FIX_STARTED",
                cycle=review_cycle,
                agent=fix_spec.agent_type,
                role="FIX",
                model=fix_spec.model,
                requested_model=fix_spec.model,
            )
            fix_result = await _run_stage(
                state=state,
                state_mgr=state_mgr,
                agent_runner=agent_runner,
                stage=Stage.FIX.value,
                role="FIX",
                spec=fix_spec,
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

            state_mgr.write_output(
                f"fix_cycle_{review_cycle}", fix_spec.agent_type, fix_result.output_text
            )
            state_mgr.append_event(
                "FIX_DONE",
                cycle=review_cycle,
                agent=fix_spec.agent_type,
                role="FIX",
                model=fix_result.model,
                requested_model=fix_result.requested_model,
                effective_model=fix_result.effective_model,
            )

            # -- TEST (post-fix) --
            state.stage = Stage.TEST.value
            state.stage_statuses[Stage.TEST.value] = StageStatus.DONE.value
            state_mgr.save_state(state)
            state_mgr.append_event(
                "TEST_DONE",
                stage=Stage.TEST.value,
                cycle=review_cycle,
                agent=fix_spec.agent_type,
                role="FIX",
            )

            # Update changed files after fix
            try:
                current_head = _get_head(worktree_path)
                state.changed_files = changed_files(
                    state.base_commit, current_head, worktree_path
                )
            except (OSError, subprocess.CalledProcessError):
                state.changed_files = []
        else:
            return _block(
                state,
                state_mgr,
                Stage.REVIEW.value,
                f"Unknown review verdict: {verdict}",
            )

    # Final CHECK verification before checkpoint
    if state.test_status == CheckStatus.FAIL.value:
        return _block(
            state,
            state_mgr,
            Stage.CHECK.value,
            "Cannot proceed to FINAL: System CHECK is FAIL",
        )

    # -- Checkpoint (최종 PASS 이후에만 생성) --
    try:
        cp = checkpoint_worktree(worktree_path, task_id, "pipeline")
        state.checkpoint = cp
        state.checkpoints["pipeline"] = cp
        state_mgr.append_event("CHECKPOINT_CREATED", label="pipeline", commit=cp)
    except RuntimeError:
        # Mock tests or environments where worktree had no uncommitted/new files
        pass

    # -- FINAL --
    state.stage = Stage.FINAL.value
    state.stage_statuses[Stage.FINAL.value] = StageStatus.DONE.value
    state.status = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("FINAL", status="DONE")

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
    role: str = "",
    spec: ProtocolAgentSpec | None = None,
    logical_agent: str | None = None,
    prompt: str,
    cwd: str,
    timeout_sec: int,
) -> AgentResult:
    """Run a single agent stage and record the result."""
    if spec is None:
        spec = ProtocolAgentSpec(agent_type=logical_agent or "codex")
    role_name = role or stage
    agent_type = spec.agent_type
    state.stage = stage
    state.role = role_name
    state.logical_agent = spec.agent_type
    state.backend_agent = agent_type
    state.requested_model = spec.model
    state.model = spec.model
    state.stage_statuses[stage] = StageStatus.RUNNING.value
    state_mgr.save_state(state)
    state_mgr.append_event(
        f"{stage}_STARTED",
        stage=stage,
        role=role_name,
        logical_agent=spec.agent_type,
        backend_agent=agent_type,
        model=spec.model,
        requested_model=spec.model,
    )

    result = await agent_runner.run_agent(
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        timeout_sec=timeout_sec,
        model=spec.model,
        effort=spec.effort,
        # agent_type (codex/claude/antigravity), not role_name — role_name is
        # nearly always identical to stage (see callers), which made this
        # window name redundant ("DESIGN-DESIGN") without ever showing which
        # actual AI backend is running.
        label=f"{agent_type}-{stage}",
    )
    result.agent = spec.agent_type
    result.agent_type = agent_type
    result.stage = stage
    result.role = role_name
    if result.requested_model is None:
        result.requested_model = spec.model
    effective_model = result.resolve_effective_model(spec.model)
    result.model = effective_model
    result.effective_model = effective_model

    state.model = effective_model
    state.requested_model = result.requested_model
    state.effective_model = effective_model

    status = StageStatus.DONE.value if result.success else StageStatus.FAILED.value
    state.stage_statuses[stage] = status
    state.handoffs.append(
        {
            "stage": stage,
            "role": role_name,
            "agent": spec.agent_type,
            "agent_type": agent_type,
            "model": state.model,
            "requested_model": state.requested_model,
            "effective_model": state.effective_model,
            "success": result.success,
            "exit_code": result.exit_code,
            "review_verdict": result.review_verdict,
            "spawned": result.spawned,
            "failure_classification": result.failure_classification,
        }
    )
    state_mgr.save_state(state)
    state_mgr.append_event(
        f"{stage}_FINISHED",
        stage=stage,
        role=role_name,
        agent=spec.agent_type,
        agent_type=agent_type,
        model=state.model,
        requested_model=state.requested_model,
        effective_model=state.effective_model,
        success=result.success,
        spawned=result.spawned,
        failure_classification=result.failure_classification,
    )

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
        waived_status = CheckStatus.WAIVED.value
        state.test_status = waived_status
        state.stage_statuses[Stage.CHECK.value] = StageStatus.DONE.value
        state_mgr.save_state(state)
        state_mgr.append_event("CHECK_RESULT", status=waived_status, count=0)
        return waived_status

    results = runner.run_all(unique)
    state.checks = [r.to_dict() for r in results]

    status = evaluate_checks(results).value
    state.test_status = status
    state.stage_statuses[Stage.CHECK.value] = StageStatus.DONE.value
    state_mgr.save_state(state)
    state_mgr.append_event("CHECK_RESULT", status=status, count=len(results))
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
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=worktree_path,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _block(
    state: ProtocolState, state_mgr: ProtocolStateManager, stage: str, reason: str
) -> ProtocolState:
    """Move the state to BLOCKED and persist."""
    state.stage = stage
    state.status = StageStatus.BLOCKED.value
    state.stage_statuses[stage] = StageStatus.BLOCKED.value
    state.blocker = reason
    state_mgr.save_state(state)
    state_mgr.append_event("BLOCKED", stage=stage, reason=reason)
    return state
