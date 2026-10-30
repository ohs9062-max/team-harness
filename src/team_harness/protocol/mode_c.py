"""MODE C — ROLE PIPELINE state machine.

Drives DESIGN → IMPLEMENT → TEST → System CHECK → REVIEW
(defaults to Claude DESIGN → Codex IMPLEMENT → Gemini/antigravity REVIEW)
on top of the existing team-harness Agent execution engine.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import UTC
import json
from pathlib import Path
import re
import signal
import subprocess
from threading import Lock
import time
from typing import Any
from typing import cast
from typing import Protocol as TypingProtocol
import uuid

from team_harness import workspace_archive
from team_harness.agents import spawner
from team_harness.agents.api_error_classifier import classify_agent_failure
from team_harness.agents.context_graph import ContextGraph
from team_harness.agents.manager import AgentManager
from team_harness.agents.manager import AgentState
from team_harness.agents.process_identity import signal_group
from team_harness.agents.rate_limits import detect_rate_limit_from_path
from team_harness.agents.rate_limits import RateLimitCircuitBreaker
from team_harness.agents.rate_limits import RateLimitTrip
from team_harness.agents.registry import resolve_template
from team_harness.agents.session_capture import extract_session_id
from team_harness.agents.tmux_view import tmux_available
from team_harness.agents.tmux_view import TmuxViewer
from team_harness.config import Config
from team_harness.decisions.advisor import advise_model_tier
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
from team_harness.protocol.usage import combine_usages
from team_harness.protocol.usage import extract_usage
from team_harness.protocol.usage import WorkerUsage
from team_harness.protocol.worker_output import extract_final_text
from team_harness.protocol.worktree import changed_files
from team_harness.protocol.worktree import checkpoint_worktree
from team_harness.protocol.worktree import create_worktree
from team_harness.protocol.worktree import diff_worktree
from team_harness.rollover_paths import RolloverMonitor
from team_harness.rollover_paths import wait_with_rollover
from team_harness.stop_policy import classify_stop_text
from team_harness.stop_policy import continuation_prompt
from team_harness.stop_policy import remember_done
from team_harness.tracking.models import DecisionRecord
from team_harness.tracking.run_log import RunLogWriter
from team_harness.worker_context import ContextMeasurement
from team_harness.worker_context import ContextRequest
from team_harness.worker_context import decide
from team_harness.worker_context import measure_agent_context
from team_harness.worker_context import registry_path_for_workspace
from team_harness.worker_context import rollover_decision
from team_harness.worker_context import WorkerContextRegistry


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


async def invoke_agent_runner(
    runner: AgentRunner,
    *,
    agent_type: str,
    prompt: str,
    cwd: str,
    timeout_sec: int,
    model: str | None = None,
    effort: str | None = None,
    label: str | None = None,
    task_key: str | None = None,
    follow_up: str = "normal",
    stage: str | None = None,
    lane: str | None = None,
    context_budget: int | None = None,
    state_mgr: Any | None = None,
    state: Any | None = None,
) -> AgentResult:
    """Invoke runner.run_agent with backward-compatible keyword argument filtering."""
    if isinstance(runner, TeamHarnessAgentRunner) or getattr(
        runner, "_accepts_worker_context", False
    ):
        return await cast(Any, runner).run_agent(
            agent_type=agent_type,
            prompt=prompt,
            cwd=cwd,
            timeout_sec=timeout_sec,
            model=model,
            effort=effort,
            label=label,
            task_key=task_key,
            follow_up=follow_up,
            stage=stage,
            lane=lane,
            context_budget=context_budget,
            state_mgr=state_mgr,
            state=state,
        )
    return await runner.run_agent(
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        timeout_sec=timeout_sec,
        model=model,
        effort=effort,
        label=label,
    )


def _safe_cleanup(runner: Any) -> None:
    """Safely terminate all worker processes registered in runner."""
    cleanup_fn = getattr(runner, "cleanup", None)
    if callable(cleanup_fn):
        cleanup_fn()
        return
    manager = getattr(runner, "manager", None)
    if manager is not None:
        kill_all_fn = getattr(manager, "kill_all", None)
        if callable(kill_all_fn):
            kill_all_fn()


class TeamHarnessAgentRunner:
    """AgentRunner implementation that delegates to team_harness execution engine."""

    _accepts_worker_context: bool = True

    def __init__(
        self,
        *,
        config: Config | None = None,
        manager: AgentManager | None = None,
        log_dir: str | Path | None = None,
        tmux_session: str | None = None,
        verbose: bool = True,
        breaker: RateLimitCircuitBreaker | None = None,
        context_graph: ContextGraph | None = None,
        advice_workspace: str = "",
    ) -> None:
        self.config = config or Config()
        self._advice_settings = config.decision_router if config is not None else None
        self._advice_workspace = advice_workspace
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
        # Optional code graph (TH-D22): one build per worktree per run, shared
        # by every stage that runs there. Off unless the config enables it.
        self.context_graph = context_graph or ContextGraph(self.config.context_graph)
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
        self._advice_lock = Lock()
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

    def _record_advice(self, record: DecisionRecord) -> None:
        # A resumed protocol may already have run metadata. Preserve it rather
        # than constructing a new writer that overwrites the existing file.
        from team_harness.tracking.persistence import write_json_atomic

        with self._advice_lock:
            log_path = self.log_dir / "run.json"
            if not log_path.exists():
                RunLogWriter(
                    run_id=f"protocol_{uuid.uuid4().hex[:12]}",
                    run_dir=self.log_dir,
                    provider=self.config.provider,
                    model=self.config.model,
                    api_base=self.config.api_base,
                )
            payload = json.loads(log_path.read_text(encoding="utf-8"))
            payload.setdefault("decisions", []).append(record.model_dump(mode="json"))
            write_json_atomic(path=log_path, payload=payload)

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
        task_key: str | None = None,
        follow_up: str = "normal",
        stage: str | None = None,
        lane: str | None = None,
        context_budget: int | None = None,
        state_mgr: Any | None = None,
        state: Any | None = None,
        **extra_kwargs: Any,
    ) -> AgentResult:
        from team_harness.brief_check import log_brief_warnings

        log_brief_warnings(prompt)
        resolve_template(agent_type=agent_type, config=self.config)

        trip = self.breaker.active_trip(agent_type)
        if trip is not None:
            return self._rate_limited_result(
                agent_type=agent_type, requested_model=model, label=label, trip=trip
            )

        context_settings = self.config.worker_context
        use_context = (
            context_settings.enabled
            and not context_settings.agent_disabled(agent_type)
            and task_key is not None
        )

        accumulated_duration = 0.0
        accumulated_usages: list[WorkerUsage | dict[str, Any] | None] = []
        current_prompt = prompt
        current_follow_up = follow_up
        rollover_generation = 0
        max_rollovers = 3
        auto_continues = 0
        auto_resume_sid: str | None = None
        monitor = RolloverMonitor(
            Path(cwd),
            context_budget or context_settings.soft_cap_tokens,
            rollover_decision,
        )

        context_registry = (
            WorkerContextRegistry(registry_path_for_workspace(cwd))
            if use_context
            else None
        )

        spawn_result: spawner.SpawnResult | None = None
        stdout_path: Path | None = None
        stderr_path: Path | None = None
        stdout_text: str = ""
        stderr_text: str = ""
        returncode: int = 0
        timed_out: bool = False
        success: bool = False
        error_msg: str | None = None
        classification: dict[str, Any] | None = None

        while True:
            spawn_mode = "fresh"
            resume_session_id: str | None = None
            context_session_id: str | None = None
            if auto_resume_sid is not None:
                spawn_mode = "resume"
                resume_session_id = auto_resume_sid
                auto_resume_sid = None

            if use_context and context_registry is not None and task_key is not None:
                dispatch_request = ContextRequest(
                    task_key=task_key,
                    agent_type=agent_type,
                    follow_up=current_follow_up,
                    phase="dispatch",
                    lane=lane,
                    stage=stage,
                    context_budget=context_budget,
                )
                dispatch_decision = decide(
                    dispatch_request,
                    snapshot={"candidates": context_registry.sessions()},
                    settings=context_settings,
                )
                if (
                    spawn_mode != "resume"
                    and dispatch_decision.action == "resume"
                    and rollover_generation == 0
                ):
                    spawn_mode = "resume"
                    resume_session_id = dispatch_decision.session_id
                context_session_id = resume_session_id or str(uuid.uuid4())
                try:
                    context_registry.register_active(
                        {
                            "task_key": task_key,
                            "session_id": context_session_id,
                            "agent_type": agent_type,
                            "model": model,
                            "worktree": cwd,
                            "worklog_path": str(Path(cwd) / "WORKLOG.md"),
                            "pending_launch": True,
                        }
                    )
                except Exception:
                    pass

                if state_mgr is not None:
                    state_mgr.append_event(
                        "WORKER_CONTEXT_DISPATCH",
                        task_key=task_key,
                        stage=stage,
                        lane=lane,
                        action=dispatch_decision.action,
                        reason=dispatch_decision.reason,
                        session_id=context_session_id,
                        generation=rollover_generation,
                    )

            baseline_head: str | None = None
            baseline_status: str | None = None
            try:
                proc = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc.returncode == 0:
                    baseline_head = proc.stdout.strip()
                proc_stat = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if proc_stat.returncode == 0:
                    baseline_status = proc_stat.stdout
            except Exception:
                pass

            started = time.monotonic()
            agent_id = f"{agent_type}_{uuid.uuid4().hex[:8]}"
            stdout_path = self.log_dir / f"{agent_id}_stdout.log"
            stderr_path = self.log_dir / f"{agent_id}_stderr.log"

            prompt_to_spawn, graph_env = await self.context_graph.prepare_spawn(
                agent_type=agent_type, prompt=current_prompt, cwd=cwd
            )
            template = resolve_template(agent_type=agent_type, config=self.config)
            await asyncio.to_thread(
                advise_model_tier,
                current_prompt,
                agent_type,
                model or template.default_model,
                effort or template.reasoning_effort,
                settings=self._advice_settings,
                workspace=self._advice_workspace,
                path="protocol.role_spawn",
                task=task_key or cwd,
                record=self._record_advice,
            )
            spawn_result = await spawner.spawn(
                agent_id=agent_id,
                agent_type=agent_type,
                prompt=prompt_to_spawn,
                cwd=Path(cwd),
                config=self.config,
                log_dir=self.log_dir,
                extra_env=graph_env or None,
                model=model,
                effort=effort,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                mode=spawn_mode,
                resume_session_id=resume_session_id,
                generated_uuid=context_session_id,
            )

            if (
                use_context
                and context_registry is not None
                and task_key is not None
                and context_session_id is not None
            ):
                try:
                    context_registry.register_active(
                        {
                            "task_key": task_key,
                            "session_id": context_session_id,
                            "agent_type": agent_type,
                            "model": spawn_result.effective_model,
                            "pid": spawn_result.pid,
                            "pgid": spawn_result.pgid,
                            "process_starttime": spawn_result.starttime,
                            "worktree": cwd,
                            "worklog_path": str(Path(cwd) / "WORKLOG.md"),
                        }
                    )
                    context_registry.cleanup(context_settings)
                except Exception:
                    pass

            agent_state = AgentState(
                id=agent_id,
                agent_type=agent_type,
                prompt=prompt_to_spawn,
                cwd=cwd,
                proc=spawn_result.proc,
                spawn_time=datetime.now(UTC),
                stdout_log=stdout_path,
                stderr_log=stderr_path,
                pgid=spawn_result.pgid,
                effective_model=spawn_result.effective_model,
                context_registry_path=context_registry.path
                if context_registry
                else None,
                context_session_id=context_session_id,
                template=spawn_result.template,
                pre_generated_uuid=spawn_result.generated_uuid,
            )
            self.manager.register(agent_state)
            if self.tmux_viewer is not None:
                await self.tmux_viewer.open_log_window(
                    window_name=label or agent_id, log_path=stdout_path
                )

            timed_out = False
            forced_rollover = False
            try:
                if use_context:
                    forced_rollover = await wait_with_rollover(
                        self.manager, agent_state, monitor, timeout_sec
                    )
                else:
                    await asyncio.wait_for(
                        spawn_result.proc.wait(), timeout=timeout_sec
                    )
            except TimeoutError:
                timed_out = True
                if spawn_result.pgid:
                    signal_group(spawn_result.pgid, signal.SIGKILL)
                elif spawn_result.proc.returncode is None:
                    spawn_result.proc.kill()
                await spawn_result.proc.wait()
            except BaseException:
                if spawn_result.pgid:
                    signal_group(spawn_result.pgid, signal.SIGKILL)
                elif spawn_result.proc.returncode is None:
                    spawn_result.proc.kill()
                try:
                    await spawn_result.proc.wait()
                except Exception:
                    pass
                raise

            duration = time.monotonic() - started
            accumulated_duration += duration
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

            p_usage = extract_usage(stdout_text)
            accumulated_usages.append(p_usage)

            success = (returncode == 0) and not timed_out
            error_msg = None
            if timed_out:
                error_msg = f"Agent timed out after {timeout_sec}s"
            elif returncode != 0:
                suffix = f": {stderr_text[:500]}" if stderr_text else ""
                error_msg = f"Agent process exited with code {returncode}{suffix}"

            classification = None
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

            is_rollover = False
            if (
                use_context
                and context_registry is not None
                and task_key is not None
                and context_session_id is not None
            ):
                tmpl = resolve_template(agent_type=agent_type, config=self.config)
                captured_id = extract_session_id(
                    tmpl, stdout_text.encode("utf-8"), context_session_id
                )
                effective_sid = captured_id or context_session_id
                measurement = measure_agent_context(
                    agent_type=agent_type, session_id=effective_sid
                )
                if (
                    measurement.measurement_status != "measured"
                    and p_usage is not None
                    and p_usage.input_tokens is not None
                ):
                    measurement = ContextMeasurement(
                        input_tokens=p_usage.input_tokens,
                        measurement_status="measured",
                        source="usage",
                    )
                boundary_request = ContextRequest(
                    task_key=task_key,
                    agent_type=agent_type,
                    follow_up=current_follow_up,
                    phase="boundary",
                    lane=lane,
                    stage=stage,
                    context_budget=context_budget,
                )
                boundary_decision = decide(
                    boundary_request,
                    snapshot={
                        "input_tokens": measurement.input_tokens,
                        "measurement_status": measurement.measurement_status,
                        "worklog_path": str(Path(cwd) / "WORKLOG.md"),
                    },
                    settings=context_settings,
                )
                park_reason = (
                    "rollover"
                    if boundary_decision.action == "rollover"
                    else ("failed" if not success else None)
                )
                try:
                    context_registry.update_measurement_and_park(
                        context_session_id,
                        measurement,
                        resolved_session_id=effective_sid,
                        reason=park_reason,
                    )
                except Exception:
                    pass

                if state_mgr is not None:
                    state_mgr.append_event(
                        "WORKER_CONTEXT_BOUNDARY",
                        task_key=task_key,
                        stage=stage,
                        lane=lane,
                        action=boundary_decision.action,
                        reason=boundary_decision.reason,
                        input_tokens=measurement.input_tokens,
                        measurement_status=measurement.measurement_status,
                        session_id=effective_sid,
                        generation=rollover_generation,
                    )

                if state is not None:
                    state.worker_context[stage or lane or task_key] = {
                        "task_key": task_key,
                        "session_id": effective_sid,
                        "action": boundary_decision.action,
                        "reason": boundary_decision.reason,
                        "input_tokens": measurement.input_tokens,
                        "measurement_status": measurement.measurement_status,
                        "generation": rollover_generation,
                    }

                if (
                    not forced_rollover
                    and success
                    and boundary_decision.action == "rollover"
                    and rollover_generation < max_rollovers
                ):
                    current_head: str | None = None
                    current_status: str | None = None
                    try:
                        p = subprocess.run(
                            ["git", "rev-parse", "HEAD"],
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if p.returncode == 0:
                            current_head = p.stdout.strip()
                        p_diff = subprocess.run(
                            ["git", "status", "--porcelain"],
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if p_diff.returncode == 0:
                            current_status = p_diff.stdout
                    except Exception:
                        pass

                    has_progress = (
                        baseline_head is not None and current_head != baseline_head
                    ) or (current_status != baseline_status)
                    if not has_progress and rollover_generation > 0:
                        if state_mgr is not None:
                            state_mgr.append_event(
                                "WORKER_CONTEXT_PROGRESS_STALLED",
                                task_key=task_key,
                                stage=stage,
                                lane=lane,
                                reason="worker context cap reached without progress; split the task further",
                            )
                        success = False
                        error_msg = "worker context cap reached without progress; split the task further"
                    else:
                        is_rollover = True
                        rollover_generation += 1
                        monitor.count = rollover_generation
                        current_follow_up = "normal"
                        if state_mgr is not None:
                            state_mgr.append_event(
                                "WORKER_CONTEXT_ROLLOVER",
                                task_key=task_key,
                                stage=stage,
                                lane=lane,
                                rollover_generation=rollover_generation,
                            )
                        current_prompt = (
                            prompt + "\n\n[Context Rollover Notice]\n"
                            "이전 워커가 단계 경계에서 맥락 상한에 도달하여 WORKLOG.md를 갱신하고 종료했습니다. "
                            "WORKLOG.md의 체크포인트와 다음 할 일부터 작업을 이어서 계속 진행하십시오."
                        )

            if forced_rollover:
                current_prompt = monitor.prompt(prompt)
                current_follow_up = "normal"
                rollover_generation += 1
                continue
            if is_rollover:
                continue

            final_text = extract_final_text(stdout_text)
            failure = classify_agent_failure(
                stderr_text=stderr_text, stdout_text=stdout_text
            )
            if (
                not timed_out
                and failure is None
                and (
                    classify_stop_text(final_text, Path(cwd))
                    if (Path(cwd) / "DONE").is_file()
                    or (Path(cwd) / "FAILED").is_file()
                    else classify_stop_text(final_text)
                )
                == "self_stop"
            ):
                if auto_continues < 2:
                    auto_continues += 1
                    remember_done(Path(cwd))
                    current_prompt = continuation_prompt(
                        prompt, final_text, auto_continues
                    )
                    sid = extract_session_id(
                        spawn_result.template,
                        stdout_text.encode(),
                        spawn_result.generated_uuid,
                    )
                    measurement = (
                        measure_agent_context(agent_type=agent_type, session_id=sid)
                        if sid
                        else None
                    )
                    if (
                        measurement
                        and measurement.measurement_status == "measured"
                        and measurement.input_tokens is not None
                        and measurement.input_tokens <= 90000
                    ):
                        auto_resume_sid = sid
                    if state_mgr is not None:
                        state_mgr.append_event(
                            "AUTO_CONTINUE",
                            task_key=task_key,
                            count=auto_continues,
                            reason=final_text,
                        )
                    continue
                success = False
                error_msg = "자동 계속 소진 — 작업 분할 필요"

            if (
                agent_state.failure_classification
                and agent_state.failure_classification.get("category") == "split_needed"
            ):
                error_msg = "작업 분할 필요"
                classification = agent_state.failure_classification

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

            break

        final_usage = combine_usages(accumulated_usages)
        effective_mod = spawn_result.effective_model if spawn_result else None
        return AgentResult(
            agent=agent_type,
            agent_type=agent_type,
            model=effective_mod,
            requested_model=model,
            effective_model=effective_mod,
            stage=stage or "",
            success=success,
            exit_code=returncode,
            stdout_path=str(stdout_path) if stdout_path else "",
            stderr_path=str(stderr_path) if stderr_path else "",
            output_text=extract_final_text(stdout_text),
            duration_sec=accumulated_duration,
            error_message=error_msg,
            failure_classification=classification,
            usage=final_usage,
        )

    def cleanup(self) -> None:
        """Terminate all workers registered in the manager."""
        self.manager.kill_all()

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
    """Compact model+effort label, e.g. "gpt-6.1-sol"/"medium" -> "sol-medium".

    Strips the "gpt-6.1-" and legacy "gpt-5.6-" prefixes (codex's naming scheme, where the
    tier name alone is unambiguous); other agents' model strings (claude,
    antigravity) are shown as-is since they aren't in that family.
    """

    if model is None:
        return "default"
    short = model.removeprefix("gpt-6.1-").removeprefix("gpt-5.6-")
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
    workspace_archive.archive_on_start(Path(target_repo))
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
        warning_msg = (
            f"Repository has tracked uncommitted changes (proceeding from HEAD): "
            f"{preflight.status_lines[:5]}"
        )
        state.warnings.append(warning_msg)
        state_mgr.append_event(
            "git_preflight.warning",
            warning=warning_msg,
            status_lines=preflight.status_lines[:5],
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
            symlink_dirs=proto_cfg.symlink_ignored_dirs,
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

    pipeline = state.worktrees.get("pipeline")
    worktree_path = (pipeline or {}).get("path", "")
    if not pipeline or not worktree_path:
        failed_stage = state.stage
        reason = state.blocker or "pre-worktree setup stage"
        raise ValueError(
            f"Cannot resume MODE C run {state.task_id}: worktree was never created "
            f"(run failed before WORKTREE_SETUP at stage {failed_stage}: {reason}). "
            "Start a fresh run with `th protocol run` instead."
        )

    stage = from_stage or _first_unfinished_stage(state)
    if stage not in RESUMABLE_MODE_C_STAGES:
        raise ValueError(
            f"Cannot resume MODE C at {stage!r}. "
            f"Resumable stages: {list(RESUMABLE_MODE_C_STAGES)}"
        )

    if not Path(worktree_path).is_dir():
        return _block(
            state,
            state_mgr,
            stage,
            f"Task worktree is missing ({worktree_path}); "
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
            if candidate in (Stage.TEST.value, Stage.CHECK.value):
                return Stage.IMPLEMENT.value
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

    task_key = f"{state.task_id or 'protocol'}/{stage}"
    try:
        result = await invoke_agent_runner(
            agent_runner,
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
            task_key=task_key,
            stage=stage,
            state_mgr=state_mgr,
            state=state,
        )
    except BaseException as exc:
        state.status = StageStatus.BLOCKED.value
        state.stage_statuses[stage] = StageStatus.BLOCKED.value
        state.blocker = (
            f"Cancelled or failed during {stage}: {type(exc).__name__}: {exc}"
        )
        state_mgr.save_state(state)
        state_mgr.append_event(
            f"{stage}_INTERRUPTED", stage=stage, error=f"{type(exc).__name__}: {exc}"
        )
        _safe_cleanup(agent_runner)
        raise
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
            "duration_sec": result.duration_sec,
            "usage": result.usage,
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
