import asyncio
from collections.abc import Awaitable
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING
import uuid

from team_harness.agents import spawner
from team_harness.agents.api_error_classifier import classify_agent_failure
from team_harness.agents.context_graph import ContextGraph
from team_harness.agents.manager import AgentState
from team_harness.agents.rate_limits import detect_rate_limit_from_path
from team_harness.agents.rate_limits import RateLimitCircuitBreaker
from team_harness.agents.rate_limits import RateLimitTrip
from team_harness.agents.registry import check_harness_depth
from team_harness.agents.registry import get_allowed_types
from team_harness.agents.registry import resolve_template
from team_harness.agents.session_capture import capture_session_id_from_path
from team_harness.agents.session_capture import extract_session_id
from team_harness.agents.template import AgentTemplate
from team_harness.agents.template import template_supports_effort
from team_harness.caller_contract import build_nested_caller_context
from team_harness.caller_contract import CallerContext
from team_harness.caller_contract import INHERITED_CALLER_CONTEXT_ENV
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER
from team_harness.decisions.advisor import advise_failure
from team_harness.decisions.advisor import advise_model_tier
from team_harness.protocol.worker_output import extract_final_text
from team_harness.rollover_paths import RolloverMonitor
from team_harness.rollover_paths import wait_with_rollover
from team_harness.stop_policy import classify_stop_text
from team_harness.stop_policy import continuation_prompt
from team_harness.stop_policy import remember_done
from team_harness.tracking.models import AgentRecord
from team_harness.tracking.models import RateLimitedFamilyRecord
from team_harness.tracking.persistence import write_json_atomic
from team_harness.tracking.worker_sessions import resume_info_for_agent_type
from team_harness.worker_context import ContextRequest
from team_harness.worker_context import dispatch as dispatch_worker_context
from team_harness.worker_context import measure_agent_context
from team_harness.worker_context import RegistryConflictError
from team_harness.worker_context import rollover_decision
from team_harness.worker_context import WorkerContextRegistry

if TYPE_CHECKING:
    from team_harness.agents.manager import AgentManager
    from team_harness.config import Config
    from team_harness.tracking.run_log import RunLogWriter
    from team_harness.ui.console import ConsoleBase

_manager: "AgentManager | None" = None
_run_log: "RunLogWriter | None" = None
_config: "Config | None" = None
_ui: "ConsoleBase | None" = None
_rate_limit_breaker: RateLimitCircuitBreaker | None = None
_session_output_dir: str = ""
_context_graph: ContextGraph = ContextGraph()

_output_cursors: dict[str, int] = {}
_output_locks: dict[str, asyncio.Lock] = {}
_wait_stdout_cursors: dict[str, int] = {}
_wait_stderr_cursors: dict[str, int] = {}
# Keep incremental stdout reads bounded so one stale cursor cannot flood the
# coordinator context with an entire long-running worker log.
READ_NEW_AGENT_OUTPUT_MAX_BYTES = 64 * 1024
WORKER_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SPAWN_AGENT_KEYS = {
    "type",
    "prompt",
    "cwd",
    "model",
    "effort",
    "mode",
    "resume_from_agent_id",
    "resume_from_session_id",
    "flags",
    "env",
    "agents",
    "worker_label",
    "delegated_role",
    "delegated_task_id",
    "expected_outputs",
    "state_responsibility",
    "task_key",
    "follow_up",
    "context_budget",
}

_READ_NEW_TRUNCATION_TEMPLATE = (
    "[read_new_agent_output truncated: omitted {omitted_bytes} of "
    "{total_new_bytes} new stdout bytes; showing latest {returned_bytes} bytes. "
    "Full stdout log: {stdout_path}]\n"
)

_READ_AGENT_OUTPUT_TRUNCATION_TEMPLATE = (
    "[read_agent_output truncated: requested tail_bytes={requested_bytes} "
    "clamped to {effective_bytes}; showing the latest {effective_bytes} bytes "
    "per stream. Full stdout log: {stdout_path} | Full stderr log: "
    "{stderr_path}]\n"
)

# Default ceiling for read_agent_output(tail_bytes=...) when no run config is
# bound (module-level tools before setup, or config missing the knob).
READ_AGENT_OUTPUT_MAX_TAIL_BYTES = 16 * 1024


def _read_output_tail(path: Path, tail_bytes: int) -> tuple[str, int]:
    """Return (decoded tail, full file size) for one log path."""
    if not path.exists():
        return "", 0
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - tail_bytes))
        return handle.read().decode(errors="replace"), size


def _render_agent_output(
    *,
    stdout_log: Path,
    stderr_log: Path,
    requested_tail_bytes: int,
    max_tail_bytes: int,
) -> str:
    """Read bounded stdout/stderr tails and prepend a banner when the request
    was clamped or the underlying logs were larger than the returned tail."""
    effective = min(max(0, requested_tail_bytes), max(0, max_tail_bytes))
    stdout_text, stdout_size = _read_output_tail(stdout_log, effective)
    stderr_text, stderr_size = _read_output_tail(stderr_log, effective)
    clamped = requested_tail_bytes > max_tail_bytes
    truncated = stdout_size > effective or stderr_size > effective
    banner = ""
    if clamped or truncated:
        banner = _READ_AGENT_OUTPUT_TRUNCATION_TEMPLATE.format(
            requested_bytes=requested_tail_bytes,
            effective_bytes=effective,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
        )
    return f"{banner}=== stdout ===\n{stdout_text}\n=== stderr ===\n{stderr_text}"


def _build_worker_output_footer(
    output_dir: str = "", config: "Config | None" = None
) -> str:
    template = (
        getattr(config, "worker_footer", DEFAULT_WORKER_FOOTER)
        if config
        else DEFAULT_WORKER_FOOTER
    )
    return template.format(session_output_dir=output_dir)


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _tail_text(path: Path, n_chars: int) -> str:
    if not path.exists():
        return ""
    with path.open("rb") as handle:
        size = path.stat().st_size
        handle.seek(max(0, size - n_chars * 2))
        return handle.read().decode(errors="replace")[-n_chars:]


def _worker_log_paths(
    *, run_dir: Path, agent_id: str, worker_label: str | None, session_output_dir: str
) -> tuple[Path, Path]:
    output_root = (
        Path(session_output_dir).expanduser().resolve()
        if session_output_dir
        else run_dir.expanduser().resolve()
    )
    label = _worker_log_label(agent_id=agent_id, worker_label=worker_label)
    worker_dir = (output_root / "workers" / label).resolve()
    if not worker_dir.is_relative_to(output_root):
        msg = f"worker log directory escaped session output directory: {worker_dir}"
        raise ValueError(msg)
    return worker_dir / "stdout.jsonl", worker_dir / "stderr.log"


def _worker_log_label(*, agent_id: str, worker_label: str | None) -> str:
    if worker_label is None:
        return agent_id
    label = worker_label.strip()
    if not WORKER_LABEL_PATTERN.fullmatch(label):
        msg = (
            "worker_label must start with a letter or digit and contain only "
            "letters, digits, '.', '_', or '-'"
        )
        raise ValueError(msg)
    return f"{label}__{agent_id}"


def _agent_output_paths(
    *, run_dir: Path, agent_id: str, session_output_dir: str
) -> tuple[Path, Path]:
    """Return the canonical per-agent output and assignment paths."""

    output_root = (
        Path(session_output_dir).expanduser().resolve()
        if session_output_dir
        else run_dir.expanduser().resolve()
    )
    output_dir = (output_root / "agents" / agent_id).resolve()
    if not output_dir.is_relative_to(output_root):
        msg = f"agent output directory escaped session output directory: {output_dir}"
        raise ValueError(msg)
    return output_dir, output_dir / "agent_assignment.json"


def _optional_spawn_string(kwargs: Mapping[str, object], name: str) -> str | None:
    """Read one optional non-blank delegation metadata string."""

    value = kwargs.get(name)
    if value is None:
        return None
    text = str(value)
    if not text.strip():
        raise ValueError(f"{name} must not be blank when provided")
    return text


def _expected_outputs(kwargs: Mapping[str, object]) -> list[str]:
    """Validate and normalize the coordinator's expected-output labels."""

    value = kwargs.get("expected_outputs")
    if value is None:
        return []
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError("expected_outputs must be an array of strings")
    outputs = [str(item) for item in value]
    if any(not output.strip() for output in outputs):
        raise ValueError("expected_outputs entries must not be blank")
    return outputs


def _build_direct_spawn_footer(
    *,
    assignment_path: Path,
    output_dir: Path,
    caller_context: CallerContext | None,
    delegated_role: str | None,
    delegated_task_id: str | None,
    expected_outputs: list[str],
    state_responsibility: str | None,
    parent_harness_run_id: str,
) -> str:
    """Render the automatic absolute-path and lineage context for a worker."""

    context_lines = [
        "# Automatic direct-spawn assignment context",
        "",
        f"Your assignment envelope (absolute): {assignment_path}",
        f"Your output directory (absolute): {output_dir}",
        f"Parent harness run id: {parent_harness_run_id}",
        "Read the assignment envelope before working and use the absolute paths it declares.",
        "You are an ephemeral delegate. Report your result to the harness coordinator; ",
        "the coordinator owns integration and the loop-layer decision.",
        "End your stdout with a result card of at most 15 lines: outcome, key "
        "decisions, files changed, and absolute paths to any longer report you "
        "wrote to a file. Write long reports to files, not stdout.",
    ]
    if caller_context is not None:
        context_lines.extend(
            [
                f"Parent loop assignment (absolute): {caller_context.parent_assignment_path}",
                f"Parent attempt id: {caller_context.parent_attempt_id}",
                f"Root/current session: {caller_context.root_session_id} / {caller_context.session_id}",
                f"Session depth and workflow role: {caller_context.session_depth} / {caller_context.workflow_role}",
            ]
        )
    if delegated_role is not None:
        context_lines.append(f"Delegated role: {delegated_role}")
    if delegated_task_id is not None:
        context_lines.append(f"Delegated task id: {delegated_task_id}")
    if state_responsibility is not None:
        context_lines.append(f"State responsibility: {state_responsibility}")
    if expected_outputs:
        context_lines.append("Expected outputs:")
        context_lines.extend(f"- {output}" for output in expected_outputs)
    return "\n".join(context_lines)


def _prepare_agent_assignment(
    *,
    agent_id: str,
    prompt: str,
    run_log: "RunLogWriter",
    config: "Config",
    run_dir: Path,
    session_output_dir: str,
    caller_context: CallerContext | None,
    kwargs: Mapping[str, object],
) -> tuple[str, Path, str | None, str | None, list[str], str | None]:
    """Persist one direct assignment and return its effective spawn metadata."""

    delegated_role = _optional_spawn_string(kwargs=kwargs, name="delegated_role")
    delegated_task_id = _optional_spawn_string(kwargs=kwargs, name="delegated_task_id")
    expected_outputs = _expected_outputs(kwargs=kwargs)
    state_responsibility = _optional_spawn_string(
        kwargs=kwargs, name="state_responsibility"
    )
    output_dir, assignment_path = _agent_output_paths(
        run_dir=run_dir, agent_id=agent_id, session_output_dir=session_output_dir
    )
    automatic_footer = _build_direct_spawn_footer(
        assignment_path=assignment_path,
        output_dir=output_dir,
        caller_context=caller_context,
        delegated_role=delegated_role,
        delegated_task_id=delegated_task_id,
        expected_outputs=expected_outputs,
        state_responsibility=state_responsibility,
        parent_harness_run_id=run_log.run_id,
    )
    parts = [prompt.rstrip()]
    if config.worker_suffix:
        parts.append(config.worker_suffix)
    parts.append(automatic_footer)
    # Keep the established output footer last: existing coordinator prompts
    # and tests rely on its final-position emphasis.
    parts.append(
        _build_worker_output_footer(output_dir=session_output_dir, config=config)
    )
    full_prompt = "\n\n".join(part for part in parts if part)
    write_json_atomic(
        path=assignment_path,
        payload={
            "schema_version": 1,
            "actor_kind": "spawned_agent",
            "agent_id": agent_id,
            "parent_harness_run_id": run_log.run_id,
            "parent_attempt_id": (
                caller_context.parent_attempt_id if caller_context is not None else None
            ),
            "root_session_id": (
                caller_context.root_session_id if caller_context is not None else None
            ),
            "session_id": (
                caller_context.session_id if caller_context is not None else None
            ),
            "session_depth": (
                caller_context.session_depth if caller_context is not None else None
            ),
            "workflow_role": (
                caller_context.workflow_role if caller_context is not None else None
            ),
            "delegated_role": delegated_role,
            "delegated_task_id": delegated_task_id,
            "delegated_objective": prompt,
            "assignment_path": str(assignment_path),
            "parent_assignment_path": (
                str(caller_context.parent_assignment_path)
                if caller_context is not None
                else None
            ),
            "output_dir": str(output_dir),
            "relevant_state_paths": (
                [str(path) for path in caller_context.relevant_state_paths]
                if caller_context is not None
                else []
            ),
            "capability_roster_path": (
                str(caller_context.capability_roster_path)
                if caller_context is not None
                and caller_context.capability_roster_path is not None
                else None
            ),
            "capability_roster_sha256": (
                caller_context.capability_roster_sha256
                if caller_context is not None
                else None
            ),
            "capability_roster_summary": (
                caller_context.capability_roster_summary
                if caller_context is not None
                else None
            ),
            "expected_outputs": expected_outputs,
            "state_responsibility": state_responsibility,
            "authored_prompt": prompt,
            "effective_prompt": full_prompt,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    return (
        full_prompt,
        assignment_path,
        delegated_role,
        delegated_task_id,
        expected_outputs,
        state_responsibility,
    )


def _validate_spawn_agent_kwargs(kwargs: Mapping[str, object]) -> None:
    """Reject coordinator-supplied fields outside the public spawn schema."""

    unknown = sorted(set(kwargs) - SPAWN_AGENT_KEYS)
    if unknown:
        msg = f"unknown spawn_agent fields: {', '.join(unknown)}"
        raise ValueError(msg)


def _resolve_resume_session_id(
    *, manager: "AgentManager", agent_type: str, kwargs: Mapping[str, object]
) -> tuple[str | None, str | None]:
    """Resolve a same-run agent reference to its captured provider session.

    ``resume_from_agent_id`` is the safe live-run interface: the coordinator
    supplies an agent id returned by ``spawn_agent``/``list_agents``, and the
    harness resolves the vendor session id it captured in memory. Raw
    ``resume_from_session_id`` remains available for ids read from a finalized
    earlier run. Invalid or ambiguous requests fail before a subprocess or
    agent record is created.
    """

    raw_agent_id = kwargs.get("resume_from_agent_id")
    raw_session_id = kwargs.get("resume_from_session_id")
    if raw_agent_id is not None and raw_session_id is not None:
        return (
            None,
            "ERROR: resume_from_agent_id and resume_from_session_id are "
            "mutually exclusive",
        )
    if raw_agent_id is None:
        if raw_session_id is not None and str(kwargs.get("mode", "fresh")) != "resume":
            return None, "ERROR: resume_from_session_id requires mode='resume'"
        return (str(raw_session_id) if raw_session_id is not None else None, None)

    if str(kwargs.get("mode", "fresh")) != "resume":
        return None, "ERROR: resume_from_agent_id requires mode='resume'"
    source_agent_id = str(raw_agent_id).strip()
    if not source_agent_id:
        return None, "ERROR: resume_from_agent_id must be a non-empty agent id"

    manager.poll_exit_codes()
    try:
        source = manager.get(agent_id=source_agent_id)
    except KeyError:
        return (
            None,
            f"ERROR: resume source agent {source_agent_id!r} does not exist "
            "in this harness run",
        )
    if source.agent_type != agent_type:
        return (
            None,
            f"ERROR: resume source agent {source_agent_id!r} has type "
            f"{source.agent_type!r}, not requested type {agent_type!r}",
        )
    if source.proc.returncode is None or source.status == "running":
        return (
            None,
            f"ERROR: resume source agent {source_agent_id!r} is still running; "
            "wait for it to become terminal before resuming",
        )
    if source.session_id is None:
        return (
            None,
            f"ERROR: resume source agent {source_agent_id!r} has no captured "
            "provider session id yet; wait for session capture or use "
            "resume_from_session_id from a finalized worker_sessions.json",
        )
    return source.session_id, None


def _inherit_nested_caller_context(
    *,
    extra_env: dict[str, str],
    agent_type: str,
    caller_context: CallerContext | None,
    parent_harness_run_id: str,
    assignment_path: Path,
) -> None:
    """Propagate outer identity only to a nested team-harness coordinator."""

    if agent_type != "harness" or caller_context is None:
        return
    nested = build_nested_caller_context(
        context=caller_context,
        parent_harness_run_id=parent_harness_run_id,
        agent_assignment_path=assignment_path,
        agent_output_dir=assignment_path.parent,
    )
    # Generated identity wins over a free-form coordinator-provided env
    # mapping. This records context; it does not constrain the nested
    # coordinator's delegation choices.
    extra_env[INHERITED_CALLER_CONTEXT_ENV] = nested.model_dump_json()


def _check_effort_supported(
    *, agent_type: str, effort: str | None, flags: list[str] | None, config: "Config"
) -> str | None:
    """Return a coordinator-visible ERROR string when an effort override
    cannot take effect as requested. Silently dropping or double-rendering
    the override would defeat the point of asking for it (the coordinator
    believes it escalated when it didn't, and the audit trail would lie)."""
    if effort is None:
        return None
    if not effort.strip():
        return (
            "ERROR: effort must be a non-empty level (e.g. low, medium, "
            "high); omit the argument to use the template default"
        )
    try:
        template = resolve_template(agent_type=agent_type, config=config)
    except ValueError:
        # Unknown agent type: let the spawn path raise its usual error.
        return None
    if not template_supports_effort(template=template):
        return (
            f"ERROR: agent type {agent_type!r} does not support a "
            "reasoning-effort override (its template declares no "
            "reasoning_effort_flag with an {effort} placeholder); respawn "
            "without effort"
        )
    conflict = _conflicting_effort_flag(template=template, flags=flags)
    if conflict is not None:
        return (
            f"ERROR: flags entry {conflict!r} would collide with the rendered "
            f"effort={effort!r} tokens; pass the level through effort only"
        )
    return None


def _conflicting_effort_flag(
    *, template: AgentTemplate, flags: list[str] | None
) -> str | None:
    """Find a caller flag that carries the same reasoning-effort option the
    explicit effort argument renders — the command would then contain the
    option twice, and whichever the CLI honors, the audit record would be
    wrong for the other."""
    if not flags:
        return None
    tokens = template.reasoning_effort_flag
    for index, token in enumerate(tokens):
        if "{effort}" not in token:
            continue
        prefix = token.split("{effort}", 1)[0]
        if prefix:
            for flag in flags:
                if flag.startswith(prefix):
                    return flag
        elif index > 0:
            option = tokens[index - 1]
            if option in flags:
                return option
    return None


def _read_new_stdout_chunk(
    *,
    stdout_log: Path,
    output_cursor: int,
    seen_stdout_cursor: int = 0,
    max_bytes: int = READ_NEW_AGENT_OUTPUT_MAX_BYTES,
) -> tuple[int, bytes, int, int]:
    """Read the unread stdout tail and return the cursor advanced to EOF.

    The unread span starts at the explicit read cursor. If that span is larger than
    max_bytes, only the latest max_bytes are returned, while the next cursor is
    still advanced to the current file size so the omitted backlog is not
    replayed on a later read.
    """
    if not stdout_log.exists():
        return output_cursor, b"", 0, 0
    size = stdout_log.stat().st_size
    cursor = output_cursor
    if size <= cursor:
        return cursor, b"", 0, 0
    total_new_bytes = size - cursor
    returned_bytes = min(max(0, max_bytes), total_new_bytes)
    omitted_bytes = total_new_bytes - returned_bytes
    with stdout_log.open("rb") as handle:
        handle.seek(size - returned_bytes)
        data = handle.read(returned_bytes)
    return size, data, omitted_bytes, total_new_bytes


def _format_new_stdout_chunk(
    *, data: bytes, omitted_bytes: int, total_new_bytes: int, stdout_log: Path
) -> str:
    """Decode a stdout chunk and prepend truncation metadata when needed."""
    text = data.decode("utf-8", errors="replace")
    if omitted_bytes <= 0:
        return text
    return (
        _READ_NEW_TRUNCATION_TEMPLATE.format(
            omitted_bytes=omitted_bytes,
            total_new_bytes=total_new_bytes,
            returned_bytes=len(data),
            stdout_path=stdout_log,
        )
        + text
    )


def _classify_if_failed(state: AgentState) -> dict | None:
    """Compute and cache failure classification for a failed agent.

    Reads the tail of stderr and stdout and runs the API error classifier.
    Returns the cached dict if already classified, or None if the failure
    does not look like an API error.
    """
    if state.failure_classification is not None:
        return state.failure_classification
    if state.exit_code is None or state.exit_code == 0:
        return None
    stderr_text = _tail_text(state.stderr_log, 4000)
    stdout_text = _tail_text(state.stdout_log, 4000)
    result = classify_agent_failure(stderr_text=stderr_text, stdout_text=stdout_text)
    if result is not None:
        state.failure_classification = {
            "is_api_error": result.is_api_error,
            "category": result.category,
            "detail": result.detail,
            "suggested_action": (
                "Respawn this task with a different agent type. "
                "See the API Error Failover Protocol."
            ),
        }
    return state.failure_classification


def _sync_finished_rate_limits(
    *,
    manager: "AgentManager",
    run_log: "RunLogWriter",
    breaker: RateLimitCircuitBreaker,
) -> None:
    """Detect and persist hard rate limits for newly terminal workers once."""

    manager.poll_exit_codes()
    for state in manager.list_all():
        if state.exit_code is None or state.rate_limit_checked:
            continue
        if not breaker.enabled:
            continue
        try:
            signal = detect_rate_limit_from_path(state.stdout_log)
        except (OSError, UnicodeError):
            continue
        # A transient open/read failure must remain retryable. Only a complete,
        # successful scan (including one that finds no signal) consumes it.
        state.rate_limit_checked = True
        if state.exit_code == 0:
            # Process success is an additional terminal guard. In ordinary
            # streams the successful result already clears provisional events;
            # this also fails closed against a truncated success record.
            continue
        if signal is None:
            continue
        trip = breaker.trip(
            family=state.agent_type, model=state.effective_model, signal=signal
        )
        if trip is None:
            continue
        run_log.record_rate_limited_family(
            RateLimitedFamilyRecord(
                family=trip.family,
                model=trip.model,
                tripped_at=trip.tripped_at,
                resets_at=trip.resets_at,
                reason=trip.reason,
            )
        )
        state.failure_classification = {
            "is_api_error": True,
            "category": "rate_limit",
            "detail": trip.reason,
            "family": trip.family,
            "model": trip.model,
            "resets_at": trip.resets_at.isoformat(),
            "suggested_action": (
                "Choose a different available agent family. This family is "
                f"blocked until {trip.resets_at.isoformat()}."
            ),
        }


def _agent_availability_payload(
    *, allowed_types: list[str], config: "Config", breaker: RateLimitCircuitBreaker
) -> dict[str, object]:
    """Render stable coordinator-facing availability without changing list_agents."""

    active = breaker.active_trips()
    families: list[dict[str, object]] = []
    available_families: list[str] = []
    rate_limited_families: list[dict[str, str | None]] = []
    for family in allowed_types:
        trip = active.get(family)
        if trip is not None:
            trip_payload = trip.as_dict()
            families.append({"status": "rate_limited", **trip_payload})
            rate_limited_families.append(trip_payload)
            continue
        template = resolve_template(agent_type=family, config=config)
        families.append(
            {"family": family, "model": template.default_model, "status": "available"}
        )
        available_families.append(family)
    return {
        "circuit_breaker_enabled": breaker.enabled,
        "available_families": available_families,
        "rate_limited_families": rate_limited_families,
        "families": families,
    }


def _rate_limited_spawn_result(
    *,
    trip: RateLimitTrip,
    requested_model: str | None,
    allowed_types: list[str],
    config: "Config",
    breaker: RateLimitCircuitBreaker,
) -> str:
    availability = _agent_availability_payload(
        allowed_types=allowed_types, config=config, breaker=breaker
    )
    return json.dumps(
        {
            "spawned": False,
            "status": "rate_limited",
            **trip.as_dict(),
            "requested_model": requested_model,
            "message": (
                f"Agent family {trip.family!r} is rate-limited until "
                f"{trip.resets_at.isoformat()}; choose a different available family."
            ),
            "available_families": availability["available_families"],
        }
    )


def _seconds_since_last_output(stdout_log: Path, stderr_log: Path) -> float | None:
    mtimes: list[float] = []
    for path in (stdout_log, stderr_log):
        try:
            mtimes.append(path.stat().st_mtime)
        except FileNotFoundError:
            continue
    if not mtimes:
        return None
    return max(0.0, datetime.now(timezone.utc).timestamp() - max(mtimes))


def _build_running_snapshot(
    state: AgentState,
    *,
    advance_cursors: bool,
    stdout_cursors: dict[str, int] | None = None,
    stderr_cursors: dict[str, int] | None = None,
) -> dict[str, object]:
    """Build a wait_for_any status snapshot and optionally advance cursors.

    Cursor stores are injectable so per-run tool bindings keep their own state,
    while the module-level tool functions can share the global stores. Advancing
    the stdout cursor here tracks delta bytes produced between wait_for_any calls.
    """
    stdout_cursor_store = (
        stdout_cursors if stdout_cursors is not None else _wait_stdout_cursors
    )
    stderr_cursor_store = (
        stderr_cursors if stderr_cursors is not None else _wait_stderr_cursors
    )
    now = datetime.now(timezone.utc)
    elapsed = int((now - state.spawn_time).total_seconds())
    stdout_total = _file_size(state.stdout_log)
    stderr_total = _file_size(state.stderr_log)
    stdout_prev = stdout_cursor_store.get(state.id, 0)
    stderr_prev = stderr_cursor_store.get(state.id, 0)
    stdout_delta = max(0, stdout_total - stdout_prev)
    stderr_delta = max(0, stderr_total - stderr_prev)
    if advance_cursors:
        stdout_cursor_store[state.id] = stdout_total
        stderr_cursor_store[state.id] = stderr_total
    last_output_age = _seconds_since_last_output(state.stdout_log, state.stderr_log)
    is_alive = state.proc.returncode is None
    stderr_tail = _tail_text(state.stderr_log, 400)

    if not is_alive:
        advisory = "Process has exited. Read the full output with read_agent_output."
    elif stderr_delta > 0 or stdout_delta > 0:
        advisory = (
            f"HEALTHY — producing output. {stdout_delta} new stdout bytes and "
            f"{stderr_delta} new stderr bytes since last check. "
            "DO NOT kill. Re-enter wait_for_any."
        )
    elif last_output_age is not None and last_output_age < 120:
        advisory = (
            f"HEALTHY — output file was touched {int(last_output_age)}s ago. "
            "DO NOT kill. Re-enter wait_for_any."
        )
    else:
        advisory = (
            f"QUIET — no new output for {int(last_output_age) if last_output_age else -1}s. "
            "Investigate with read_agent_output before considering kill_agent."
        )

    snapshot: dict[str, object] = {
        "agent_id": state.id,
        "agent_type": state.agent_type,
        "status": _status_from_state(state),
        "elapsed_seconds": elapsed,
        "stdout_bytes_total": stdout_total,
        "stderr_bytes_total": stderr_total,
        "stdout_bytes_delta_since_last_check": stdout_delta,
        "stderr_bytes_delta_since_last_check": stderr_delta,
        "seconds_since_last_output": int(last_output_age)
        if last_output_age is not None
        else None,
        "is_alive": is_alive,
        "recent_stderr_tail": stderr_tail,
        "advisory": advisory,
    }
    if not is_alive:
        classification = _classify_if_failed(state)
        if classification is not None:
            snapshot["failure_classification"] = classification
    return snapshot


def _patience_policy(config: "Config | None") -> dict[str, object]:
    floor = float(
        getattr(config, "min_agent_lifetime_before_kill_s", 600.0) if config else 600.0
    )
    return {
        "min_wait_before_kill_seconds": floor,
        "stderr_growth_free_kill_window_seconds": 120,
        "typical_durations": {
            "codex_planning": "20-45 min",
            "gemini_research": "15-30 min",
            "claude_review": "5-15 min",
        },
        "rationale": (
            "timed_out=true means the agents are STILL RUNNING. This is the expected "
            "and normal outcome for any non-trivial task. Re-enter wait_for_any with "
            "a longer timeout. Only consider kill_agent if the agent has been quiet "
            "for >= 20 minutes AND you have read the full output and confirmed the "
            "trajectory is wrong."
        ),
    }


def _should_refuse_kill(
    state: AgentState, *, min_lifetime_s: float
) -> tuple[bool, dict[str, object]]:
    # A zero (or negative) floor disables the patience backstop entirely —
    # this is the escape hatch for tests and users who need legacy behavior.
    if min_lifetime_s <= 0:
        snapshot = _build_running_snapshot(state, advance_cursors=False)
        return False, snapshot
    snapshot = _build_running_snapshot(state, advance_cursors=False)
    elapsed = int(snapshot["elapsed_seconds"])  # type: ignore[arg-type]
    age = snapshot["seconds_since_last_output"]
    stderr_delta = int(snapshot["stderr_bytes_delta_since_last_check"])  # type: ignore[arg-type]
    too_young = elapsed < min_lifetime_s
    actively_producing = (isinstance(age, int) and age < 120) or stderr_delta > 0
    if too_young:
        reason = (
            f"Agent is only {elapsed}s old (floor is {int(min_lifetime_s)}s). "
            "Re-enter wait_for_any and inspect stderr/stdout before escalating. "
            "If stderr is growing, the agent is healthy — continue waiting."
        )
        return True, {
            "killed": False,
            "refused": True,
            "reason": reason,
            "snapshot": snapshot,
        }
    if actively_producing:
        reason = (
            f"Agent produced output very recently (age={age}s, "
            f"stderr_delta={stderr_delta}). It is actively working. "
            "Re-enter wait_for_any."
        )
        return True, {
            "killed": False,
            "refused": True,
            "reason": reason,
            "snapshot": snapshot,
        }
    return False, snapshot


AGENT_STATUS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_status",
        "description": "Get current status and elapsed runtime of a spawned agent.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}
READ_AGENT_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_agent_output",
        "description": "Read stdout and stderr log tails for a spawned agent.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "string"},
                "tail_bytes": {
                    "type": "integer",
                    "description": "Maximum tail bytes to return (default 8192).",
                },
            },
            "required": ["agent_id"],
        },
    },
}
READ_NEW_AGENT_OUTPUT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_new_agent_output",
        "description": "Read newly appended stdout content for an agent since last check.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}
LIST_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_agents",
        "description": "List all spawned agents and their current statuses in this run.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
AGENT_AVAILABILITY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "agent_availability",
        "description": "Check available and rate-limited agent-template families.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
WAIT_FOR_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait_for_agents",
        "description": "Wait for specified agents to complete or until timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": [],
        },
    },
}
WAIT_FOR_ANY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "wait_for_any",
        "description": "Wait for the first agent to complete or until timeout.",
        "parameters": {
            "type": "object",
            "properties": {
                "agent_ids": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "number"},
            },
            "required": ["agent_ids"],
        },
    },
}
KILL_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "kill_agent",
        "description": "Terminate a running agent subprocess.",
        "parameters": {
            "type": "object",
            "properties": {"agent_id": {"type": "string"}},
            "required": ["agent_id"],
        },
    },
}


def setup(
    manager: "AgentManager",
    run_log: "RunLogWriter",
    config: "Config",
    ui: "ConsoleBase",
    session_output_dir: str = "",
) -> None:
    """Bind legacy module-level agent tools to one active harness run."""

    global _manager
    global _run_log
    global _config
    global _ui
    global _rate_limit_breaker
    global _session_output_dir
    global _context_graph
    _manager = manager
    _run_log = run_log
    _config = config
    _ui = ui
    _rate_limit_breaker = RateLimitCircuitBreaker(
        enabled=config.rate_limit_circuit_breaker,
        default_cooldown_s=config.rate_limit_default_cooldown_s,
    )
    _session_output_dir = session_output_dir
    _context_graph = ContextGraph(config.context_graph)
    _output_cursors.clear()
    _output_locks.clear()
    _wait_stdout_cursors.clear()
    _wait_stderr_cursors.clear()


def _require_setup() -> tuple["AgentManager", "RunLogWriter", "Config", "ConsoleBase"]:
    if _manager is None or _run_log is None or _config is None or _ui is None:
        raise RuntimeError(
            "agent_tools.setup() must be called before using agent tools"
        )
    return _manager, _run_log, _config, _ui


def _require_rate_limit_breaker() -> RateLimitCircuitBreaker:
    if _rate_limit_breaker is None:
        raise RuntimeError(
            "agent_tools.setup() must be called before using agent tools"
        )
    return _rate_limit_breaker


def _format_spawn_agent_defaults(
    *, allowed_types: list[str], config: "Config | None"
) -> str:
    """Describe active template defaults for the coordinator-facing schema.

    The spawn_agent tool accepts raw extra flags, so the schema description is
    the coordinator's only view into flags already provided by the selected
    agent template. When a run config is available, this helper renders the
    resolved templates rather than hard-coding built-in defaults.
    """
    if config is None:
        return (
            "Built-in agent templates already include their required "
            "approval/output/session flags. Use flags only for additional "
            "non-default CLI flags."
        )

    lines = ["Default CLI behavior by agent type. Do not repeat these through flags:"]
    for agent_type in allowed_types:
        template = resolve_template(agent_type=agent_type, config=config)
        details: list[str] = []
        if template.shared_flags:
            details.append("shared_flags=" + json.dumps(list(template.shared_flags)))
        if template.model_flag is not None:
            if template.default_model is not None:
                details.append(
                    f"model flag {template.model_flag!r} defaults to "
                    f"{template.default_model!r} unless spawn_agent(model=...) "
                    "overrides it"
                )
            else:
                details.append(
                    f"model flag {template.model_flag!r} is applied only when "
                    "spawn_agent(model=...) is provided"
                )
        if template.reasoning_effort_flag:
            if template.reasoning_effort is not None:
                details.append(
                    f"reasoning effort defaults to {template.reasoning_effort!r} "
                    "unless spawn_agent(effort=...) overrides it"
                )
            else:
                details.append(
                    "reasoning-effort override supported via spawn_agent(effort=...)"
                )
        if template.prompt_flag is not None:
            details.append(f"prompt flag {template.prompt_flag!r} is applied")
        if template.resume_prefix or template.resume_flags:
            resume_tokens = list(template.resume_prefix + template.resume_flags)
            details.append("resume mode adds " + json.dumps(resume_tokens))
        if template.deduplicate_flags:
            details.append(
                "duplicate standalone flags ignored="
                + json.dumps(list(template.deduplicate_flags))
            )
        if not details:
            details.append("no default flags")
        lines.append(f"- {agent_type}: " + "; ".join(details))
    lines.append("Use flags only for additional CLI flags not listed above.")
    return "\n".join(lines)


def spawn_agent_schema(
    allowed_types: list[str], config: "Config | None" = None
) -> dict:
    """Build the coordinator tool schema for dynamic direct worker spawns."""

    return {
        "type": "function",
        "function": {
            "name": "spawn_agent",
            "description": (
                "Spawn a worker agent subprocess. Returns agent_<id> on success, "
                "or rate-limited JSON status if blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": allowed_types},
                    "prompt": {"type": "string"},
                    "cwd": {"type": "string"},
                    "model": {"type": "string"},
                    "effort": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Optional reasoning-effort override (e.g. low, medium, high).",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["fresh", "resume"],
                        "description": (
                            "Use 'resume' to continue an existing provider session instead of "
                            "starting a fresh one."
                        ),
                    },
                    "resume_from_session_id": {
                        "type": "string",
                        "description": (
                            "Provider session ID from worker_sessions.json to resume. "
                            "Mutually exclusive with resume_from_agent_id."
                        ),
                    },
                    "resume_from_agent_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": (
                            "Agent ID from this live harness run to resume; "
                            "mutually exclusive with resume_from_session_id."
                        ),
                    },
                    "flags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Additional non-default CLI flags for this worker.",
                    },
                    "env": {"type": "object"},
                    "agents": {
                        "type": "array",
                        "description": "Only used when type == 'harness'.",
                        "items": {"type": "string"},
                    },
                    "worker_label": {
                        "type": "string",
                        "description": "Optional filesystem-safe worker label for log directory naming.",
                    },
                    "delegated_role": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Audit label for delegate's role (e.g. implementation, research, review).",
                    },
                    "delegated_task_id": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Task identifier linking the spawn with its result.",
                    },
                    "expected_outputs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "description": "List of concrete outputs this delegate should produce.",
                    },
                    "state_responsibility": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Statement of state updates the delegate is accountable for.",
                    },
                    "task_key": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Immutable worker-context key; enables shared rollover policy.",
                    },
                    "context_budget": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "Worker input context budget; defaults to the configured soft cap.",
                    },
                    "follow_up": {
                        "type": "string",
                        "enum": ["short", "normal"],
                        "description": "Only same-key short follow-ups may resume a parked session.",
                    },
                },
                "required": ["type", "prompt", "cwd"],
                "additionalProperties": False,
            },
        },
    }


def _resolve_spawn_cwd(raw_cwd: str, config: "Config") -> str:
    path = Path(raw_cwd).expanduser()
    if path.is_absolute():
        return str(path.resolve())
    base_cwd = Path(getattr(config, "cwd", ".")).expanduser().resolve()
    return str((base_cwd / path).resolve())


def _prepare_worker_context_dispatch(
    *,
    kwargs: Mapping[str, object],
    agent_type: str,
    cwd: str,
    model: str | None,
    run_id: str,
    run_dir: Path,
    config: "Config",
    spawn_mode: str,
    resume_session_id: str | None,
) -> tuple[
    str,
    str | None,
    str | None,
    WorkerContextRegistry | None,
    str | None,
    dict[str, object] | None,
    str | None,
]:
    """Dispatch and pre-register opt-in worker-context state before spawning."""

    raw_task_key = kwargs.get("task_key")
    raw_follow_up = kwargs.get("follow_up")
    if raw_task_key is None:
        if raw_follow_up is not None and str(raw_follow_up) not in {"short", "normal"}:
            return (
                spawn_mode,
                resume_session_id,
                None,
                None,
                None,
                None,
                "ERROR: worker context: follow_up must be 'short' or 'normal'",
            )
        return spawn_mode, resume_session_id, None, None, None, None, None
    task_key = str(raw_task_key).strip()
    follow_up = str(raw_follow_up) if raw_follow_up is not None else "normal"
    try:
        context_decision, context_registry = dispatch_worker_context(
            workspace=cwd,
            request=ContextRequest(
                task_key=task_key, agent_type=agent_type, follow_up=follow_up
            ),
            settings=config.worker_context,
        )
    except ValueError as exc:
        return (
            spawn_mode,
            resume_session_id,
            None,
            None,
            None,
            None,
            f"ERROR: worker context: {exc}",
        )
    if context_decision.reason == "worker_context_disabled":
        return spawn_mode, resume_session_id, None, None, None, None, None
    if context_decision.action == "resume":
        spawn_mode = "resume"
        resume_session_id = context_decision.session_id
    elif (
        kwargs.get("resume_from_agent_id") is None
        and kwargs.get("resume_from_session_id") is None
    ):
        spawn_mode = "fresh"
        resume_session_id = None
    context_session_id = resume_session_id or str(uuid.uuid4())
    prior_entry = next(
        (
            dict(item)
            for item in context_registry.sessions()
            if item.get("session_id") == context_session_id
        ),
        None,
    )
    try:
        context_registry.register_active(
            {
                "task_key": task_key,
                "session_id": context_session_id,
                "agent_type": agent_type,
                "model": model,
                "run_id": run_id,
                "worktree": cwd,
                "worklog_path": str(run_dir / "WORKLOG.md"),
                "pending_launch": True,
            }
        )
    except RegistryConflictError as exc:
        return (
            spawn_mode,
            resume_session_id,
            None,
            None,
            None,
            None,
            f"ERROR: worker context: {exc}",
        )
    return (
        spawn_mode,
        resume_session_id,
        task_key,
        context_registry,
        context_session_id,
        prior_entry,
        None,
    )


def _confirm_worker_context_spawn(
    *,
    context_registry: WorkerContextRegistry | None,
    context_session_id: str | None,
    task_key: str | None,
    agent_type: str,
    spawn_result: spawner.SpawnResult,
    run_id: str,
    cwd: str,
    run_dir: Path,
    config: "Config",
) -> str | None:
    """Confirm active registration with process identity and finalize retention."""

    if context_registry is None or context_session_id is None or task_key is None:
        return context_session_id
    context_registry.register_active(
        {
            "task_key": task_key,
            "session_id": context_session_id,
            "agent_type": agent_type,
            "model": spawn_result.effective_model,
            "pid": spawn_result.pid,
            "pgid": spawn_result.pgid,
            "process_starttime": spawn_result.starttime,
            "run_id": run_id,
            "worktree": cwd,
            "worklog_path": str(run_dir / "WORKLOG.md"),
        }
    )
    context_registry.cleanup(config.worker_context)
    return context_session_id


def _context_budget(kwargs: Mapping[str, object]) -> int | None:
    value = kwargs.get("context_budget")
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool) and value > 0
        else None
    )


def _install_continuation(
    state: AgentState,
    *,
    config: "Config",
    run_dir: Path,
    full_prompt: str,
    extra_env: dict[str, str],
    model: str | None,
    effort: str | None,
    flags: list[str] | None,
    agents: list[str] | None,
    manager: "AgentManager | None" = None,
    task_key: str | None = None,
    worker_output_dir: Path | None = None,
    context_budget: int | None = None,
) -> None:
    count = 0
    monitor = RolloverMonitor(
        worker_output_dir or run_dir,
        context_budget or config.worker_context.soft_cap_tokens,
        rollover_decision,
    )
    if (
        manager is not None
        and task_key
        and config.worker_context.enabled
        and not config.worker_context.agent_disabled(state.agent_type)
    ):

        async def wait_for_boundary(current: AgentState) -> bool:
            return await wait_with_rollover(manager, current, monitor)

        state.rollover_wait = wait_for_boundary

    async def continue_if_needed(current: AgentState) -> bool:
        nonlocal count
        stdout = (
            current.stdout_log.read_text(encoding="utf-8", errors="replace")
            if current.stdout_log.exists()
            else ""
        )
        stderr = (
            current.stderr_log.read_text(encoding="utf-8", errors="replace")
            if current.stderr_log.exists()
            else ""
        )
        reason = extract_final_text(stdout)
        forced = current.rollover_triggered
        current.rollover_triggered = False
        if not forced and (
            current.status == "killed"
            or classify_agent_failure(stderr_text=stderr, stdout_text=stdout)
            is not None
            or (
                classify_stop_text(reason, worker_output_dir)
                if worker_output_dir is not None
                and (
                    (worker_output_dir / "DONE").is_file()
                    or (worker_output_dir / "FAILED").is_file()
                )
                else classify_stop_text(reason)
            )
            != "self_stop"
        ):
            return False
        if not forced and count >= 2:
            current.continuation_exhausted = True
            current.failure_classification = {
                "category": "auto_continue_exhausted",
                "detail": "자동 계속 소진 — 작업 분할 필요",
            }
            return False
        if not forced:
            count += 1
            if worker_output_dir is not None:
                remember_done(worker_output_dir)
        sid = (
            extract_session_id(current.template, stdout.encode(), current.session_id)
            if current.template
            else None
        )
        measurement = (
            measure_agent_context(agent_type=current.agent_type, session_id=sid)
            if sid
            else None
        )
        resume = bool(
            not forced
            and sid
            and measurement
            and measurement.measurement_status == "measured"
            and measurement.input_tokens is not None
            and measurement.input_tokens <= 90000
        )
        result = await spawner.spawn(
            agent_id=current.id,
            agent_type=current.agent_type,
            prompt=monitor.prompt(full_prompt)
            if forced
            else continuation_prompt(full_prompt, reason, count),
            cwd=Path(current.cwd),
            config=config,
            log_dir=run_dir,
            extra_env=extra_env,
            model=model,
            effort=effort,
            extra_flags=flags,
            allowed_agents=agents if current.agent_type == "harness" else None,
            stdout_path=current.stdout_log,
            stderr_path=current.stderr_log,
            mode="resume" if resume else "fresh",
            resume_session_id=sid if resume else None,
        )
        current.proc, current.pgid = result.proc, result.pgid
        current.session_id = result.generated_uuid or (sid if resume else None)
        current.pre_generated_uuid = result.generated_uuid
        current.template = result.template
        return True

    state.continuation = continue_if_needed


async def spawn_agent(**kwargs: object) -> str:
    """Validate, record, and launch one worker for the active legacy binding."""

    _validate_spawn_agent_kwargs(kwargs=kwargs)
    manager, run_log, config, ui = _require_setup()
    agent_type = str(kwargs["type"])
    prompt = str(kwargs["prompt"])
    from team_harness.brief_check import log_brief_warnings

    log_brief_warnings(prompt)
    cwd = _resolve_spawn_cwd(str(kwargs["cwd"]), config)
    model = str(kwargs["model"]) if kwargs.get("model") is not None else None
    breaker = _require_rate_limit_breaker()
    _sync_finished_rate_limits(manager=manager, run_log=run_log, breaker=breaker)
    active_trip = breaker.active_trip(agent_type)
    if active_trip is not None:
        return _rate_limited_spawn_result(
            trip=active_trip,
            requested_model=model,
            allowed_types=get_allowed_types(config=config),
            config=config,
            breaker=breaker,
        )
    effort = str(kwargs["effort"]) if kwargs.get("effort") is not None else None
    flags = (
        [str(item) for item in kwargs.get("flags", [])]
        if kwargs.get("flags") is not None
        else None
    )
    raw_env = kwargs.get("env")
    env: dict[str, str] | None = None
    if isinstance(raw_env, Mapping):
        env = {str(key): str(value) for key, value in raw_env.items()}
    agents = (
        [str(item) for item in kwargs.get("agents", [])]
        if kwargs.get("agents") is not None
        else None
    )
    worker_label = (
        str(kwargs["worker_label"]) if kwargs.get("worker_label") is not None else None
    )
    if agent_type == "harness":
        try:
            check_harness_depth(config=config)
        except ValueError:
            return f"ERROR: max harness depth ({config.max_depth}) reached"
    effort_error = _check_effort_supported(
        agent_type=agent_type, effort=effort, flags=flags, config=config
    )
    if effort_error is not None:
        return effort_error
    spawn_mode = str(kwargs.get("mode", "fresh"))
    resume_session_id, resume_error = _resolve_resume_session_id(
        manager=manager, agent_type=agent_type, kwargs=kwargs
    )
    if resume_error is not None:
        return resume_error

    run_dir = config.run_dir
    if run_dir is None:
        raise RuntimeError("config.run_dir must be set before spawning agents")

    (
        spawn_mode,
        resume_session_id,
        task_key,
        context_registry,
        context_session_id,
        prior_context_entry,
        context_error,
    ) = _prepare_worker_context_dispatch(
        kwargs=kwargs,
        agent_type=agent_type,
        cwd=cwd,
        model=model,
        run_id=run_log.run_id,
        run_dir=run_dir,
        config=config,
        spawn_mode=spawn_mode,
        resume_session_id=resume_session_id,
    )
    if context_error is not None:
        return context_error

    current_depth = int(os.environ.get("TEAM_HARNESS_DEPTH", "0"))
    extra_env = {**(env or {}), "TEAM_HARNESS_DEPTH": str(current_depth + 1)}
    extra_env.pop(INHERITED_CALLER_CONTEXT_ENV, None)
    agent_id = "agent_" + uuid.uuid4().hex[:12]
    try:
        (
            full_prompt,
            assignment_path,
            delegated_role,
            delegated_task_id,
            expected_outputs,
            state_responsibility,
        ) = _prepare_agent_assignment(
            agent_id=agent_id,
            prompt=prompt,
            run_log=run_log,
            config=config,
            run_dir=run_dir,
            session_output_dir=_session_output_dir,
            caller_context=None,
            kwargs=kwargs,
        )
        # Optional code graph (TH-D22): built once per cwd per run, hint first so
        # the fixed text leads the prompt.
        full_prompt, graph_env = await _context_graph.prepare_spawn(
            agent_type=agent_type, prompt=full_prompt, cwd=cwd, mode=spawn_mode
        )
        extra_env = {**graph_env, **extra_env}
        stdout_log, stderr_log = _worker_log_paths(
            run_dir=run_dir,
            agent_id=agent_id,
            worker_label=worker_label,
            session_output_dir=_session_output_dir,
        )
        template = resolve_template(agent_type=agent_type, config=config)
        await asyncio.to_thread(
            advise_model_tier,
            prompt,
            agent_type,
            model or template.default_model,
            effort or template.reasoning_effort,
            settings=config.decision_router,
            path="agent_tools.spawn_agent",
            task=cwd,
            record=run_log.record_decision,
        )
        spawn_result = await spawner.spawn(
            agent_id=agent_id,
            agent_type=agent_type,
            prompt=full_prompt,
            cwd=Path(cwd),
            config=config,
            log_dir=run_dir,
            extra_env=extra_env,
            model=model,
            effort=effort,
            extra_flags=flags,
            allowed_agents=agents if agent_type == "harness" else None,
            stdout_path=stdout_log,
            stderr_path=stderr_log,
            mode=spawn_mode,
            resume_session_id=resume_session_id,
            generated_uuid=context_session_id,
        )
    except BaseException:
        if context_registry is not None and context_session_id is not None:
            context_registry.restore_after_failed_launch(
                context_session_id, prior_context_entry
            )
        raise
    context_session_id = _confirm_worker_context_spawn(
        context_registry=context_registry,
        context_session_id=context_session_id,
        task_key=task_key,
        agent_type=agent_type,
        spawn_result=spawn_result,
        run_id=run_log.run_id,
        cwd=cwd,
        run_dir=run_dir,
        config=config,
    )
    custom_home = extra_env.get("HOME")
    state = AgentState(
        id=agent_id,
        agent_type=agent_type,
        prompt=prompt,
        cwd=cwd,
        proc=spawn_result.proc,
        spawn_time=datetime.now(timezone.utc),
        stdout_log=stdout_log,
        stderr_log=stderr_log,
        session_id=spawn_result.generated_uuid,
        effective_model=spawn_result.effective_model,
        pgid=spawn_result.pgid,
        template=spawn_result.template,
        pre_generated_uuid=spawn_result.generated_uuid,
        context_registry_path=(
            context_registry.path if context_registry is not None else None
        ),
        context_session_id=context_session_id,
        codex_sessions_root=(
            Path(custom_home) / ".codex" / "sessions" if custom_home else None
        ),
    )
    _install_continuation(
        state,
        config=config,
        run_dir=run_dir,
        full_prompt=full_prompt,
        extra_env=extra_env,
        model=model,
        effort=effort,
        flags=flags,
        agents=agents,
        manager=manager,
        task_key=task_key,
        worker_output_dir=assignment_path.parent,
        context_budget=_context_budget(kwargs),
    )
    manager.register(state=state)
    record = AgentRecord(
        id=agent_id,
        agent_type=agent_type,
        status=state.status,
        cwd=cwd,
        prompt=prompt,
        full_prompt=full_prompt,
        command=spawn_result.command,
        spawned_at=state.spawn_time,
        stdout_log=str(stdout_log),
        stderr_log=str(stderr_log),
        session_id=state.session_id,
        pid=spawn_result.pid,
        pgid=spawn_result.pgid,
        starttime=spawn_result.starttime,
        requested_model=model,
        requested_effort=effort,
        effective_model=spawn_result.effective_model,
        effective_effort=spawn_result.effective_effort,
        assignment_path=str(assignment_path),
        delegated_role=delegated_role,
        delegated_task_id=delegated_task_id,
        expected_outputs=expected_outputs,
        state_responsibility=state_responsibility,
    )
    run_log.record_agent_spawn(record=record)
    ui.agent_event(event="spawned", state=state)
    done_event = asyncio.Event()
    watch_task = asyncio.create_task(
        _watch_agent(agent_id=agent_id, done_event=done_event)
    )
    capture_task = asyncio.create_task(
        _capture_session_id_task(
            agent_id=agent_id,
            template=spawn_result.template,
            pre_generated_uuid=spawn_result.generated_uuid,
            stop_event=done_event,
        )
    )
    manager.track_finalization_task(task=watch_task)
    manager.track_finalization_task(task=capture_task)
    return agent_id


async def _watch_agent(agent_id: str, done_event: asyncio.Event) -> None:
    manager, run_log, _, ui = _require_setup()
    breaker = _require_rate_limit_breaker()
    try:
        exit_code = await manager.wait_one(agent_id)
        state = manager.get(agent_id)
        if state.status != "killed":
            state.status = "done" if exit_code == 0 else "failed"
            _sync_finished_rate_limits(
                manager=manager, run_log=run_log, breaker=breaker
            )
            if state.status == "failed":
                _classify_if_failed(state)
            run_log.update_agent(
                agent_id,
                exit_code=exit_code,
                finished_at=state.finished_at or datetime.now(timezone.utc),
                status=state.status,
                session_id=state.session_id,
            )
            ui.agent_event(event="done" if exit_code == 0 else "failed", state=state)
    finally:
        done_event.set()


async def _capture_session_id_task(
    *,
    agent_id: str,
    template: AgentTemplate,
    pre_generated_uuid: str | None,
    stop_event: asyncio.Event,
) -> None:
    """Capture and persist a provider session id through the worker's final tail."""

    manager, run_log, _, _ = _require_setup()
    state = manager.get(agent_id)
    try:
        session_id = await capture_session_id_from_path(
            stdout_path=state.stdout_log,
            template=template,
            pre_generated_uuid=pre_generated_uuid,
            stop_event=stop_event,
            max_wait_s=24 * 60 * 60,
        )
    except (OSError, UnicodeError):
        session_id = None
    if session_id is not None:
        state.session_id = session_id
        if (
            state.context_registry_path is not None
            and state.context_session_id is not None
            and not state.context_parked
        ):
            WorkerContextRegistry(state.context_registry_path).rekey_active_session(
                state.context_session_id, session_id
            )
            state.context_session_id = session_id
        run_log.update_agent(agent_id, session_id=session_id)


def _status_from_state(state: AgentState) -> str:
    if state.status == "killed":
        return "killed"
    if state.exit_code is None:
        return "running"
    if state.exit_code == 0:
        return "done (exit 0)"
    return f"failed (exit {state.exit_code})"


async def agent_status(agent_id: str) -> str:
    manager, run_log, _, _ = _require_setup()
    _sync_finished_rate_limits(
        manager=manager, run_log=run_log, breaker=_require_rate_limit_breaker()
    )
    return _status_from_state(manager.get(agent_id))


async def read_agent_output(agent_id: str, tail_bytes: int = 8192) -> str:
    manager, _, config, _ = _require_setup()
    state = manager.get(agent_id)
    max_tail_bytes = int(
        getattr(config, "read_output_max_tail_bytes", READ_AGENT_OUTPUT_MAX_TAIL_BYTES)
    )
    return await asyncio.to_thread(
        _render_agent_output,
        stdout_log=state.stdout_log,
        stderr_log=state.stderr_log,
        requested_tail_bytes=tail_bytes,
        max_tail_bytes=max_tail_bytes,
    )


async def read_new_agent_output(agent_id: str) -> str:
    manager, _, config, _ = _require_setup()
    state = manager.get(agent_id)
    max_bytes = int(
        getattr(config, "read_new_output_max_bytes", READ_NEW_AGENT_OUTPUT_MAX_BYTES)
    )
    lock = _output_locks.setdefault(agent_id, asyncio.Lock())
    async with lock:
        cursor = _output_cursors.get(agent_id, 0)

        new_cursor, data, omitted_bytes, total_new_bytes = await asyncio.to_thread(
            _read_new_stdout_chunk,
            stdout_log=state.stdout_log,
            output_cursor=cursor,
            max_bytes=max_bytes,
        )
        if not data:
            return ""
        _output_cursors[agent_id] = new_cursor
        return _format_new_stdout_chunk(
            data=data,
            omitted_bytes=omitted_bytes,
            total_new_bytes=total_new_bytes,
            stdout_log=state.stdout_log,
        )


async def list_agents() -> str:
    manager, run_log, _, _ = _require_setup()
    _sync_finished_rate_limits(
        manager=manager, run_log=run_log, breaker=_require_rate_limit_breaker()
    )
    payload = []
    for state in manager.list_all():
        payload.append(
            {
                "id": state.id,
                "type": state.agent_type,
                "status": _status_from_state(state),
                "cwd": state.cwd,
                "elapsed": _elapsed(state),
            }
        )
    return json.dumps(payload)


async def agent_availability() -> str:
    manager, run_log, config, _ = _require_setup()
    breaker = _require_rate_limit_breaker()
    _sync_finished_rate_limits(manager=manager, run_log=run_log, breaker=breaker)
    return json.dumps(
        _agent_availability_payload(
            allowed_types=get_allowed_types(config=config),
            config=config,
            breaker=breaker,
        )
    )


async def _failure_advice_lines(
    manager: "AgentManager", ids: list[str], config: "Config", run_log: "RunLogWriter"
) -> dict[str, str]:
    lines = {}
    for aid in ids:
        state = manager.get(aid)
        if state.exit_code in (None, 0) or state.status == "killed":
            continue
        advice = await asyncio.to_thread(
            advise_failure,
            state.cwd,
            failure_context=f"agent={aid} exit={state.exit_code}",
            task_text=state.prompt,
            stdout_log=state.stdout_log,
            stderr_log=state.stderr_log,
            settings=config.decision_router,
            path="agent_tools.wait_for_agents",
            record=run_log.record_decision,
        )
        if advice is not None:
            lines[aid] = advice.line()
    return lines


async def wait_for_agents(
    agent_ids: list[str] | None = None, timeout: float | None = None
) -> str:
    manager, run_log, config, _ = _require_setup()
    breaker = _require_rate_limit_breaker()
    ids = list(manager._agents) if agent_ids is None else agent_ids
    if not ids:
        return json.dumps({"agents": {}, "timed_out": False})
    try:
        await asyncio.wait_for(manager.wait_for(ids), timeout=timeout)
        _sync_finished_rate_limits(manager=manager, run_log=run_log, breaker=breaker)
        return json.dumps(
            {
                "agents": {
                    agent_id: _status_from_state(manager.get(agent_id))
                    for agent_id in ids
                },
                "timed_out": False,
                **(
                    {"jev": lines}
                    if (
                        lines := await _failure_advice_lines(
                            manager, ids, config, run_log
                        )
                    )
                    else {}
                ),
            }
        )
    except asyncio.TimeoutError:
        _sync_finished_rate_limits(manager=manager, run_log=run_log, breaker=breaker)
        return json.dumps(
            {
                "agents": {
                    agent_id: _status_from_state(manager.get(agent_id))
                    for agent_id in ids
                },
                "timed_out": True,
                **(
                    {"jev": lines}
                    if (
                        lines := await _failure_advice_lines(
                            manager, ids, config, run_log
                        )
                    )
                    else {}
                ),
            }
        )


async def wait_for_any(agent_ids: list[str], timeout: float | None = None) -> str:
    manager, _, config, _ = _require_setup()
    if not agent_ids:
        return json.dumps({"agent_id": None, "timed_out": False, "running": []})
    missing = [aid for aid in agent_ids if aid not in manager._agents]
    if missing:
        return json.dumps(
            {
                "error": f"Agent(s) not found: {', '.join(missing)}",
                "agent_id": None,
                "timed_out": False,
                "running": [],
            }
        )
    tasks = {
        asyncio.ensure_future(manager.wait_one(agent_id)): agent_id
        for agent_id in agent_ids
    }
    try:
        done, pending = await asyncio.wait_for(
            asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED),
            timeout=timeout,
        )
        for pending_task in pending:
            pending_task.cancel()
        finished_id = tasks[next(iter(done))]
        running_ids = [aid for aid in agent_ids if aid != finished_id]
        finished_state = manager.get(finished_id)
        elapsed = int(
            (datetime.now(timezone.utc) - finished_state.spawn_time).total_seconds()
        )
        result: dict[str, object] = {
            "agent_id": finished_id,
            "finished_agent_id": finished_id,
            "timed_out": False,
            "status": await agent_status(finished_id),
            "elapsed_seconds": elapsed,
            "running": [
                _build_running_snapshot(manager.get(aid), advance_cursors=True)
                for aid in running_ids
            ],
            "patience_policy": _patience_policy(config),
        }
        classification = _classify_if_failed(finished_state)
        if classification is not None:
            result["failure_classification"] = classification
        return json.dumps(result)
    except asyncio.TimeoutError:
        for task in tasks:
            task.cancel()
        return json.dumps(
            {
                "agent_id": None,
                "finished_agent_id": None,
                "timed_out": True,
                "running": [
                    _build_running_snapshot(manager.get(aid), advance_cursors=True)
                    for aid in agent_ids
                ],
                "patience_policy": _patience_policy(config),
            }
        )


async def kill_agent(agent_id: str, *, force: bool = False) -> str:
    manager, run_log, config, ui = _require_setup()
    state = manager.get(agent_id)
    if state.proc.returncode is not None:
        manager.poll_exit_codes()
        return json.dumps(
            {
                "killed": False,
                "refused": False,
                "message": f"Agent {agent_id} already finished.",
            }
        )
    if not force:
        floor = float(getattr(config, "min_agent_lifetime_before_kill_s", 600.0))
        refused, payload = _should_refuse_kill(state, min_lifetime_s=floor)
        if refused:
            return json.dumps(payload)
    manager.kill(agent_id)
    try:
        await asyncio.wait_for(state.proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        state.proc.kill()
        await state.proc.wait()
    # The leader is dead; make sure its group (helper processes) dies too.
    await manager.ensure_group_dead(agent_id)
    state.exit_code = state.proc.returncode
    state.finished_at = datetime.now(timezone.utc)
    state.status = "killed"
    run_log.update_agent(
        agent_id,
        exit_code=state.exit_code if state.exit_code is not None else -1,
        finished_at=state.finished_at,
        status="killed",
    )
    ui.agent_event(event="killed", state=state)
    return json.dumps(
        {
            "killed": True,
            "refused": False,
            "agent_id": agent_id,
            "message": f"Killed {agent_id}.",
        }
    )


def _elapsed(state: AgentState) -> str:
    end = state.finished_at or datetime.now(timezone.utc)
    total_seconds = int((end - state.spawn_time).total_seconds())
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


AGENT_TOOL_SCHEMAS = [
    (AGENT_STATUS_SCHEMA, agent_status),
    (READ_AGENT_OUTPUT_SCHEMA, read_agent_output),
    (READ_NEW_AGENT_OUTPUT_SCHEMA, read_new_agent_output),
    (LIST_AGENTS_SCHEMA, list_agents),
    (AGENT_AVAILABILITY_SCHEMA, agent_availability),
    (WAIT_FOR_AGENTS_SCHEMA, wait_for_agents),
    (WAIT_FOR_ANY_SCHEMA, wait_for_any),
    (KILL_AGENT_SCHEMA, kill_agent),
]


def build_agent_tool_bindings(
    *,
    manager: "AgentManager",
    run_log: "RunLogWriter",
    config: "Config",
    ui: "ConsoleBase",
    allowed_types: list[str],
    session_output_dir: str = "",
    caller_context: CallerContext | None = None,
) -> list[tuple[dict, Callable[..., Awaitable[str]]]]:
    """Build per-run agent tool closures for concurrent safety.

    Returns a list of (schema, async_callable) pairs. Each closure captures
    its own output_cursors and output_locks dicts so that concurrent runs
    do not share state.
    """
    output_cursors: dict[str, int] = {}
    output_locks: dict[str, asyncio.Lock] = {}
    wait_stdout_cursors: dict[str, int] = {}
    wait_stderr_cursors: dict[str, int] = {}
    rate_limit_breaker = RateLimitCircuitBreaker(
        enabled=config.rate_limit_circuit_breaker,
        default_cooldown_s=config.rate_limit_default_cooldown_s,
    )
    context_graph = ContextGraph(config.context_graph)

    async def _spawn_agent(**kwargs: object) -> str:
        """Validate, record, and launch one worker for this run-local binding."""

        _validate_spawn_agent_kwargs(kwargs=kwargs)
        agent_type = str(kwargs["type"])
        prompt = str(kwargs["prompt"])
        from team_harness.brief_check import log_brief_warnings

        log_brief_warnings(prompt)
        cwd = _resolve_spawn_cwd(str(kwargs["cwd"]), config)
        model_val = str(kwargs["model"]) if kwargs.get("model") is not None else None
        _sync_finished_rate_limits(
            manager=manager, run_log=run_log, breaker=rate_limit_breaker
        )
        active_trip = rate_limit_breaker.active_trip(agent_type)
        if active_trip is not None:
            return _rate_limited_spawn_result(
                trip=active_trip,
                requested_model=model_val,
                allowed_types=allowed_types,
                config=config,
                breaker=rate_limit_breaker,
            )
        effort_val = str(kwargs["effort"]) if kwargs.get("effort") is not None else None
        flags = (
            [str(item) for item in kwargs.get("flags", [])]
            if kwargs.get("flags") is not None
            else None
        )
        raw_env = kwargs.get("env")
        env: dict[str, str] | None = None
        if isinstance(raw_env, Mapping):
            env = {str(key): str(value) for key, value in raw_env.items()}
        agents_arg = (
            [str(item) for item in kwargs.get("agents", [])]
            if kwargs.get("agents") is not None
            else None
        )
        worker_label = (
            str(kwargs["worker_label"])
            if kwargs.get("worker_label") is not None
            else None
        )
        if agent_type == "harness":
            try:
                check_harness_depth(config=config)
            except ValueError:
                return f"ERROR: max harness depth ({config.max_depth}) reached"
        effort_error = _check_effort_supported(
            agent_type=agent_type, effort=effort_val, flags=flags, config=config
        )
        if effort_error is not None:
            return effort_error
        spawn_mode = str(kwargs.get("mode", "fresh"))
        resume_session_id, resume_error = _resolve_resume_session_id(
            manager=manager, agent_type=agent_type, kwargs=kwargs
        )
        if resume_error is not None:
            return resume_error

        run_dir = config.run_dir
        if run_dir is None:
            raise RuntimeError("config.run_dir must be set before spawning agents")

        (
            spawn_mode,
            resume_session_id,
            task_key,
            context_registry,
            context_session_id,
            prior_context_entry,
            context_error,
        ) = _prepare_worker_context_dispatch(
            kwargs=kwargs,
            agent_type=agent_type,
            cwd=cwd,
            model=model_val,
            run_id=run_log.run_id,
            run_dir=run_dir,
            config=config,
            spawn_mode=spawn_mode,
            resume_session_id=resume_session_id,
        )
        if context_error is not None:
            return context_error

        current_depth = int(os.environ.get("TEAM_HARNESS_DEPTH", "0"))
        extra_env = {**(env or {}), "TEAM_HARNESS_DEPTH": str(current_depth + 1)}
        extra_env.pop(INHERITED_CALLER_CONTEXT_ENV, None)
        agent_id = "agent_" + uuid.uuid4().hex[:12]
        try:
            (
                full_prompt,
                assignment_path,
                delegated_role,
                delegated_task_id,
                expected_outputs,
                state_responsibility,
            ) = _prepare_agent_assignment(
                agent_id=agent_id,
                prompt=prompt,
                run_log=run_log,
                config=config,
                run_dir=run_dir,
                session_output_dir=session_output_dir,
                caller_context=caller_context,
                kwargs=kwargs,
            )
            _inherit_nested_caller_context(
                extra_env=extra_env,
                agent_type=agent_type,
                caller_context=caller_context,
                parent_harness_run_id=run_log.run_id,
                assignment_path=assignment_path,
            )
            # Optional code graph (TH-D22): built once per cwd per run, hint first
            # so the fixed text leads the prompt.
            full_prompt, graph_env = await context_graph.prepare_spawn(
                agent_type=agent_type, prompt=full_prompt, cwd=cwd, mode=spawn_mode
            )
            extra_env = {**graph_env, **extra_env}
            stdout_log, stderr_log = _worker_log_paths(
                run_dir=run_dir,
                agent_id=agent_id,
                worker_label=worker_label,
                session_output_dir=session_output_dir,
            )
            template = resolve_template(agent_type=agent_type, config=config)
            await asyncio.to_thread(
                advise_model_tier,
                prompt,
                agent_type,
                model_val or template.default_model,
                effort_val or template.reasoning_effort,
                settings=config.decision_router,
                path="agent_tools.spawn_agent",
                task=cwd,
                record=run_log.record_decision,
            )
            spawn_result = await spawner.spawn(
                agent_id=agent_id,
                agent_type=agent_type,
                prompt=full_prompt,
                cwd=Path(cwd),
                config=config,
                log_dir=run_dir,
                extra_env=extra_env,
                model=model_val,
                effort=effort_val,
                extra_flags=flags,
                allowed_agents=agents_arg if agent_type == "harness" else None,
                stdout_path=stdout_log,
                stderr_path=stderr_log,
                mode=spawn_mode,
                resume_session_id=resume_session_id,
                generated_uuid=context_session_id,
            )
        except BaseException:
            if context_registry is not None and context_session_id is not None:
                context_registry.restore_after_failed_launch(
                    context_session_id, prior_context_entry
                )
            raise
        context_session_id = _confirm_worker_context_spawn(
            context_registry=context_registry,
            context_session_id=context_session_id,
            task_key=task_key,
            agent_type=agent_type,
            spawn_result=spawn_result,
            run_id=run_log.run_id,
            cwd=cwd,
            run_dir=run_dir,
            config=config,
        )
        custom_home = extra_env.get("HOME")
        state = AgentState(
            id=agent_id,
            agent_type=agent_type,
            prompt=prompt,
            cwd=cwd,
            proc=spawn_result.proc,
            spawn_time=datetime.now(timezone.utc),
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            session_id=spawn_result.generated_uuid,
            effective_model=spawn_result.effective_model,
            pgid=spawn_result.pgid,
            template=spawn_result.template,
            pre_generated_uuid=spawn_result.generated_uuid,
            context_registry_path=(
                context_registry.path if context_registry is not None else None
            ),
            context_session_id=context_session_id,
            codex_sessions_root=(
                Path(custom_home) / ".codex" / "sessions" if custom_home else None
            ),
        )
        _install_continuation(
            state,
            config=config,
            run_dir=run_dir,
            full_prompt=full_prompt,
            extra_env=extra_env,
            model=model_val,
            effort=effort_val,
            flags=flags,
            agents=agents_arg,
            manager=manager,
            task_key=task_key,
            worker_output_dir=assignment_path.parent,
            context_budget=_context_budget(kwargs),
        )
        manager.register(state=state)
        record = AgentRecord(
            id=agent_id,
            agent_type=agent_type,
            coordinator_turn_index=None,
            status=state.status,
            cwd=cwd,
            prompt=prompt,
            full_prompt=full_prompt,
            command=spawn_result.command,
            spawned_at=state.spawn_time,
            stdout_log=str(stdout_log),
            stderr_log=str(stderr_log),
            session_id=state.session_id,
            resume=resume_info_for_agent_type(agent_type=agent_type),
            pid=spawn_result.pid,
            pgid=spawn_result.pgid,
            starttime=spawn_result.starttime,
            requested_model=model_val,
            requested_effort=effort_val,
            effective_model=spawn_result.effective_model,
            effective_effort=spawn_result.effective_effort,
            assignment_path=str(assignment_path),
            delegated_role=delegated_role,
            delegated_task_id=delegated_task_id,
            expected_outputs=expected_outputs,
            state_responsibility=state_responsibility,
        )
        run_log.record_agent_spawn(record=record)
        ui.agent_event(event="spawned", state=state)

        done_event = asyncio.Event()

        async def _watch() -> None:
            try:
                exit_code = await manager.wait_one(agent_id)
                s = manager.get(agent_id)
                if s.status != "killed":
                    s.status = "done" if exit_code == 0 else "failed"
                    _sync_finished_rate_limits(
                        manager=manager, run_log=run_log, breaker=rate_limit_breaker
                    )
                    if s.status == "failed":
                        _classify_if_failed(s)
                    run_log.update_agent(
                        agent_id,
                        exit_code=exit_code,
                        finished_at=s.finished_at or datetime.now(timezone.utc),
                        status=s.status,
                        session_id=s.session_id,
                    )
                    ui.agent_event(
                        event="done" if exit_code == 0 else "failed", state=s
                    )
            finally:
                done_event.set()

        async def _capture_session() -> None:
            """Capture this worker's provider session id after its final tail."""

            try:
                session_id = await capture_session_id_from_path(
                    stdout_path=stdout_log,
                    template=spawn_result.template,
                    pre_generated_uuid=spawn_result.generated_uuid,
                    stop_event=done_event,
                    max_wait_s=24 * 60 * 60,
                )
            except (OSError, UnicodeError):
                session_id = None
            if session_id is not None:
                s = manager.get(agent_id)
                s.session_id = session_id
                if (
                    s.context_registry_path is not None
                    and s.context_session_id is not None
                    and not s.context_parked
                ):
                    WorkerContextRegistry(s.context_registry_path).rekey_active_session(
                        s.context_session_id, session_id
                    )
                    s.context_session_id = session_id
                run_log.update_agent(agent_id, session_id=session_id)

        watch_task = asyncio.create_task(_watch())
        capture_task = asyncio.create_task(_capture_session())
        manager.track_finalization_task(task=watch_task)
        manager.track_finalization_task(task=capture_task)
        return agent_id

    async def _agent_status(agent_id: str) -> str:
        _sync_finished_rate_limits(
            manager=manager, run_log=run_log, breaker=rate_limit_breaker
        )
        return _status_from_state(manager.get(agent_id))

    async def _read_agent_output(agent_id: str, tail_bytes: int = 8192) -> str:
        state = manager.get(agent_id)
        max_tail_bytes = int(
            getattr(
                config, "read_output_max_tail_bytes", READ_AGENT_OUTPUT_MAX_TAIL_BYTES
            )
        )
        return await asyncio.to_thread(
            _render_agent_output,
            stdout_log=state.stdout_log,
            stderr_log=state.stderr_log,
            requested_tail_bytes=tail_bytes,
            max_tail_bytes=max_tail_bytes,
        )

    async def _read_new_agent_output(agent_id: str) -> str:
        state = manager.get(agent_id)
        max_bytes = int(
            getattr(
                config, "read_new_output_max_bytes", READ_NEW_AGENT_OUTPUT_MAX_BYTES
            )
        )
        lock = output_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            cursor = output_cursors.get(agent_id, 0)

            new_cursor, data, omitted_bytes, total_new_bytes = await asyncio.to_thread(
                _read_new_stdout_chunk,
                stdout_log=state.stdout_log,
                output_cursor=cursor,
                max_bytes=max_bytes,
            )
            if not data:
                return ""
            output_cursors[agent_id] = new_cursor
            return _format_new_stdout_chunk(
                data=data,
                omitted_bytes=omitted_bytes,
                total_new_bytes=total_new_bytes,
                stdout_log=state.stdout_log,
            )

    async def _list_agents() -> str:
        _sync_finished_rate_limits(
            manager=manager, run_log=run_log, breaker=rate_limit_breaker
        )
        payload = []
        for state in manager.list_all():
            payload.append(
                {
                    "id": state.id,
                    "type": state.agent_type,
                    "status": _status_from_state(state),
                    "cwd": state.cwd,
                    "elapsed": _elapsed(state),
                }
            )
        return json.dumps(payload)

    async def _agent_availability() -> str:
        _sync_finished_rate_limits(
            manager=manager, run_log=run_log, breaker=rate_limit_breaker
        )
        return json.dumps(
            _agent_availability_payload(
                allowed_types=allowed_types, config=config, breaker=rate_limit_breaker
            )
        )

    async def _wait_for_agents(
        agent_ids: list[str] | None = None, timeout: float | None = None
    ) -> str:
        ids = list(manager._agents) if agent_ids is None else agent_ids
        if not ids:
            return json.dumps({"agents": {}, "timed_out": False})
        try:
            await asyncio.wait_for(manager.wait_for(ids), timeout=timeout)
            _sync_finished_rate_limits(
                manager=manager, run_log=run_log, breaker=rate_limit_breaker
            )
            return json.dumps(
                {
                    "agents": {
                        aid: _status_from_state(manager.get(aid)) for aid in ids
                    },
                    "timed_out": False,
                    **(
                        {"jev": lines}
                        if (
                            lines := await _failure_advice_lines(
                                manager, ids, config, run_log
                            )
                        )
                        else {}
                    ),
                }
            )
        except asyncio.TimeoutError:
            _sync_finished_rate_limits(
                manager=manager, run_log=run_log, breaker=rate_limit_breaker
            )
            return json.dumps(
                {
                    "agents": {
                        aid: _status_from_state(manager.get(aid)) for aid in ids
                    },
                    "timed_out": True,
                    **(
                        {"jev": lines}
                        if (
                            lines := await _failure_advice_lines(
                                manager, ids, config, run_log
                            )
                        )
                        else {}
                    ),
                }
            )

    async def _wait_for_any(agent_ids: list[str], timeout: float | None = None) -> str:
        if not agent_ids:
            return json.dumps({"agent_id": None, "timed_out": False, "running": []})
        missing = [aid for aid in agent_ids if aid not in manager._agents]
        if missing:
            return json.dumps(
                {
                    "error": f"Agent(s) not found: {', '.join(missing)}",
                    "agent_id": None,
                    "timed_out": False,
                    "running": [],
                }
            )
        tasks = {asyncio.ensure_future(manager.wait_one(aid)): aid for aid in agent_ids}
        try:
            done, pending = await asyncio.wait_for(
                asyncio.wait(tasks.keys(), return_when=asyncio.FIRST_COMPLETED),
                timeout=timeout,
            )
            for pending_task in pending:
                pending_task.cancel()
            finished_id = tasks[next(iter(done))]
            _sync_finished_rate_limits(
                manager=manager, run_log=run_log, breaker=rate_limit_breaker
            )
            finished_state = manager.get(finished_id)
            elapsed = int(
                (datetime.now(timezone.utc) - finished_state.spawn_time).total_seconds()
            )
            wait_result: dict[str, object] = {
                "agent_id": finished_id,
                "finished_agent_id": finished_id,
                "timed_out": False,
                "status": _status_from_state(finished_state),
                "elapsed_seconds": elapsed,
                "running": [
                    _build_running_snapshot(
                        manager.get(aid),
                        advance_cursors=True,
                        stdout_cursors=wait_stdout_cursors,
                        stderr_cursors=wait_stderr_cursors,
                    )
                    for aid in agent_ids
                    if aid != finished_id
                ],
                "patience_policy": _patience_policy(config),
            }
            classification = _classify_if_failed(finished_state)
            if classification is not None:
                wait_result["failure_classification"] = classification
            return json.dumps(wait_result)
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
            _sync_finished_rate_limits(
                manager=manager, run_log=run_log, breaker=rate_limit_breaker
            )
            return json.dumps(
                {
                    "agent_id": None,
                    "finished_agent_id": None,
                    "timed_out": True,
                    "running": [
                        _build_running_snapshot(
                            manager.get(aid),
                            advance_cursors=True,
                            stdout_cursors=wait_stdout_cursors,
                            stderr_cursors=wait_stderr_cursors,
                        )
                        for aid in agent_ids
                    ],
                    "patience_policy": _patience_policy(config),
                }
            )

    async def _kill_agent(agent_id: str, *, force: bool = False) -> str:
        state = manager.get(agent_id)
        if state.proc.returncode is not None:
            manager.poll_exit_codes()
            return json.dumps(
                {
                    "killed": False,
                    "refused": False,
                    "message": f"Agent {agent_id} already finished.",
                }
            )
        if not force:
            floor = float(getattr(config, "min_agent_lifetime_before_kill_s", 600.0))
            refused, payload = _should_refuse_kill(state, min_lifetime_s=floor)
            if refused:
                return json.dumps(payload)
        manager.kill(agent_id)
        try:
            await asyncio.wait_for(state.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            state.proc.kill()
            await state.proc.wait()
        # The leader is dead; make sure its group (helper processes) dies too.
        await manager.ensure_group_dead(agent_id)
        state.exit_code = state.proc.returncode
        state.finished_at = datetime.now(timezone.utc)
        state.status = "killed"
        run_log.update_agent(
            agent_id,
            exit_code=state.exit_code if state.exit_code is not None else -1,
            finished_at=state.finished_at,
            status="killed",
        )
        ui.agent_event(event="killed", state=state)
        return json.dumps(
            {
                "killed": True,
                "refused": False,
                "agent_id": agent_id,
                "message": f"Killed {agent_id}.",
            }
        )

    return [
        (spawn_agent_schema(allowed_types=allowed_types, config=config), _spawn_agent),
        (AGENT_STATUS_SCHEMA, _agent_status),
        (READ_AGENT_OUTPUT_SCHEMA, _read_agent_output),
        (READ_NEW_AGENT_OUTPUT_SCHEMA, _read_new_agent_output),
        (LIST_AGENTS_SCHEMA, _list_agents),
        (AGENT_AVAILABILITY_SCHEMA, _agent_availability),
        (WAIT_FOR_AGENTS_SCHEMA, _wait_for_agents),
        (WAIT_FOR_ANY_SCHEMA, _wait_for_any),
        (KILL_AGENT_SCHEMA, _kill_agent),
    ]
