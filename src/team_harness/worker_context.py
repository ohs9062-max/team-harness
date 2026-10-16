"""Explicit worker-context measurement, policy decisions, and session registry.

This module deliberately does not launch, resume, or stop workers.  Stage 3
will connect its explicit operations to launch/watch/protocol paths; stage 2
only makes those operations observable and testable.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import fcntl
import json
import os
from pathlib import Path
from typing import Any
from typing import Iterator
from uuid import uuid4

from team_harness.tracking.persistence import write_json_atomic

REGISTRY_SCHEMA_VERSION = 1
MEASUREMENT_STATUSES = frozenset({"measured", "estimated", "unknown"})
FOLLOW_UP_VALUES = frozenset({"short", "normal"})


class WorkerContextError(ValueError):
    """A caller supplied an invalid context-policy or registry value."""


class RegistryConflictError(WorkerContextError):
    """A registry mutation would violate the one-active-worker contract."""


@dataclass(frozen=True)
class WorkerContextSettings:
    enabled: bool = True
    soft_cap_tokens: int = 60000
    resume_max_tokens: int = 90000
    max_parked_sessions: int = 2
    parked_ttl_hours: int = 24
    disabled_agents: tuple[str, ...] = ("agy",)

    def __post_init__(self) -> None:
        if self.soft_cap_tokens <= 0:
            raise WorkerContextError("soft_cap_tokens must be greater than zero")
        if self.resume_max_tokens < self.soft_cap_tokens:
            raise WorkerContextError(
                "resume_max_tokens must be greater than or equal to soft_cap_tokens"
            )
        if self.max_parked_sessions < 1:
            raise WorkerContextError("max_parked_sessions must be at least one")
        if self.parked_ttl_hours < 1:
            raise WorkerContextError("parked_ttl_hours must be at least one")

    def agent_disabled(self, agent_type: str) -> bool:
        aliases = {agent_type}
        if agent_type == "antigravity":
            aliases.add("agy")
        return bool(aliases.intersection(self.disabled_agents))


@dataclass(frozen=True)
class ContextRequest:
    task_key: str
    agent_type: str
    follow_up: str = "normal"
    phase: str = "dispatch"
    session_id: str | None = None
    lane: str | None = None
    stage: str | None = None
    long_running_wait_requested: bool = False

    def __post_init__(self) -> None:
        if not self.task_key:
            raise WorkerContextError("task_key is required")
        if self.follow_up not in FOLLOW_UP_VALUES:
            raise WorkerContextError("follow_up must be 'short' or 'normal'")
        if self.phase not in {"dispatch", "boundary"}:
            raise WorkerContextError("phase must be 'dispatch' or 'boundary'")


@dataclass(frozen=True)
class ContextDecision:
    action: str
    reason: str
    session_id: str | None = None
    input_tokens: int | None = None
    measurement_status: str = "unknown"
    worklog_path: str | None = None
    generation: int | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ContextMeasurement:
    input_tokens: int | None
    measurement_status: str
    source: str | None = None
    source_offset: int | None = None
    call_id: str | None = None
    measured_at: str | None = None

    def __post_init__(self) -> None:
        if self.measurement_status not in MEASUREMENT_STATUSES:
            raise WorkerContextError("invalid measurement_status")
        if self.input_tokens is not None and self.input_tokens < 0:
            raise WorkerContextError("input_tokens cannot be negative")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_timestamp(value: datetime | None = None) -> str:
    return (value or utc_now()).astimezone(timezone.utc).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def registry_path_for_workspace(workspace: str | Path) -> Path:
    return (
        Path(workspace).resolve() / ".team-harness" / "worker_context" / "registry.json"
    )


def parse_task_front_matter(path: str | Path) -> tuple[str, str]:
    """Read only the leading YAML subset needed for context policy."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise WorkerContextError(f"cannot read task file {path}: {exc}") from exc
    if not lines or lines[0].strip() != "---":
        raise WorkerContextError("task file must start with YAML front matter")
    values: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key.strip() in {"task_key", "follow_up"}:
            values[key.strip()] = value.strip().strip("\"'")
    else:
        raise WorkerContextError("task file YAML front matter is not closed")
    task_key = values.get("task_key", "")
    follow_up = values.get("follow_up", "normal")
    ContextRequest(task_key=task_key, agent_type="codex", follow_up=follow_up)
    return task_key, follow_up


def _event_value(event: dict[str, object], *paths: tuple[str, ...]) -> object | None:
    for path in paths:
        value: object = event
        for key in path:
            if not isinstance(value, dict):
                break
            value = value.get(key)
        else:
            if value is not None:
                return value
    return None


def _matches_session(event: dict[str, object], session_id: str, path: Path) -> bool:
    values = (
        _event_value(
            event,
            ("session_id",),
            ("thread_id",),
            ("payload", "session_id"),
            ("payload", "thread_id"),
            ("payload", "id"),
        ),
    )
    return session_id in values or session_id in str(path)


def measure_codex_rollout(
    *, session_id: str, sessions_root: str | Path | None = None
) -> ContextMeasurement:
    """Read the latest matching Codex `token_count` current-input value.

    A rollout is accepted only when its path or an event identifies the captured
    session.  This avoids treating another Codex session's usage as resumable.
    """

    root = Path(sessions_root or Path.home() / ".codex" / "sessions")
    if not root.exists():
        return ContextMeasurement(None, "unknown")
    newest: tuple[int, int, Path, dict[str, object]] | None = None
    for rollout in root.rglob("rollout-*.jsonl"):
        try:
            with rollout.open("rb") as stream:
                offset = 0
                for raw in stream:
                    end_offset = offset + len(raw)
                    try:
                        event = json.loads(raw)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        offset = end_offset
                        continue
                    if not isinstance(event, dict) or not _matches_session(
                        event, session_id, rollout
                    ):
                        offset = end_offset
                        continue
                    tokens = _event_value(
                        event, ("payload", "info", "last_token_usage", "input_tokens")
                    )
                    if (
                        event.get("type") == "event_msg"
                        and isinstance(tokens, int)
                        and not isinstance(tokens, bool)
                    ):
                        candidate = (
                            rollout.stat().st_mtime_ns,
                            end_offset,
                            rollout,
                            event,
                        )
                        if newest is None or candidate[:2] >= newest[:2]:
                            newest = candidate
                    offset = end_offset
        except OSError:
            continue
    if newest is None:
        return ContextMeasurement(None, "unknown")
    _, offset, rollout, event = newest
    tokens = _event_value(
        event, ("payload", "info", "last_token_usage", "input_tokens")
    )
    call_id = _event_value(
        event, ("payload", "call_id"), ("payload", "info", "call_id"), ("id",)
    )
    return ContextMeasurement(
        input_tokens=int(tokens) if isinstance(tokens, int) else None,
        measurement_status="measured",
        source=str(rollout.resolve()),
        source_offset=offset,
        call_id=call_id if isinstance(call_id, str) else None,
        measured_at=utc_timestamp(),
    )


def measure_agent_context(
    *, agent_type: str, session_id: str | None, sessions_root: str | Path | None = None
) -> ContextMeasurement:
    """Measure only provider formats validated in this stage; never estimate agy."""

    if agent_type in {"agy", "antigravity"}:
        return ContextMeasurement(None, "estimated")
    if agent_type == "codex" and session_id:
        return measure_codex_rollout(session_id=session_id, sessions_root=sessions_root)
    return ContextMeasurement(None, "unknown")


def _eligible_parked(
    record: dict[str, Any],
    request: ContextRequest,
    settings: WorkerContextSettings,
    now: datetime,
) -> bool:
    parked_at = _parse_time(record.get("parked_at"))
    return (
        record.get("state") == "parked"
        and record.get("task_key") == request.task_key
        and record.get("agent_type") == request.agent_type
        and record.get("lane") == request.lane
        and record.get("stage") == request.stage
        and record.get("measurement_status") == "measured"
        and isinstance(record.get("input_tokens"), int)
        and record["input_tokens"] <= settings.resume_max_tokens
        and parked_at is not None
        and now - parked_at <= timedelta(hours=settings.parked_ttl_hours)
    )


def _decision_with_current(
    action: str,
    reason: str,
    *,
    input_tokens: int | None,
    measurement_status: str,
    worklog_path: str | None,
    generation: int | None,
) -> ContextDecision:
    return ContextDecision(
        action,
        reason,
        input_tokens=input_tokens,
        measurement_status=measurement_status,
        worklog_path=worklog_path,
        generation=generation,
    )


def decide(
    request: ContextRequest,
    snapshot: dict[str, Any] | None,
    settings: WorkerContextSettings,
    now: datetime | None = None,
) -> ContextDecision:
    """Return a policy result without reading files or changing registry state."""

    current = snapshot or {}
    evaluated_at = now or utc_now()
    tokens = current.get("input_tokens")
    if not isinstance(tokens, int) or isinstance(tokens, bool):
        tokens = None
    status = current.get("measurement_status", "unknown")
    if not isinstance(status, str) or status not in MEASUREMENT_STATUSES:
        status = "unknown"
    worklog_path = current.get("worklog_path")
    if not isinstance(worklog_path, str):
        worklog_path = None
    generation = current.get("generation")
    if not isinstance(generation, int) or isinstance(generation, bool):
        generation = None
    if not settings.enabled or settings.agent_disabled(request.agent_type):
        action = "continue" if request.phase == "boundary" else "fresh"
        return _decision_with_current(
            action,
            "worker_context_disabled",
            input_tokens=tokens,
            measurement_status=status,
            worklog_path=worklog_path,
            generation=generation,
        )
    if request.phase == "boundary":
        if request.long_running_wait_requested:
            return _decision_with_current(
                "rollover",
                "long_running_wait",
                input_tokens=tokens,
                measurement_status=status,
                worklog_path=worklog_path,
                generation=generation,
            )
        if tokens is None or status != "measured":
            return _decision_with_current(
                "continue",
                "measurement_unknown",
                input_tokens=tokens,
                measurement_status=status,
                worklog_path=worklog_path,
                generation=generation,
            )
        cap = (
            settings.resume_max_tokens
            if request.follow_up == "short"
            else settings.soft_cap_tokens
        )
        if tokens > cap:
            return _decision_with_current(
                "rollover",
                "context_cap_exceeded",
                input_tokens=tokens,
                measurement_status=status,
                worklog_path=worklog_path,
                generation=generation,
            )
        return _decision_with_current(
            "continue",
            "within_context_cap",
            input_tokens=tokens,
            measurement_status=status,
            worklog_path=worklog_path,
            generation=generation,
        )

    candidates = current.get("candidates", [])
    if request.follow_up == "short" and isinstance(candidates, list):
        eligible = [
            candidate
            for candidate in candidates
            if isinstance(candidate, dict)
            and _eligible_parked(candidate, request, settings, evaluated_at)
        ]
        if eligible:
            selected = max(
                eligible,
                key=lambda item: (
                    _parse_time(item.get("last_used_at"))
                    or _parse_time(item.get("parked_at"))
                    or datetime.min.replace(tzinfo=timezone.utc)
                ),
            )
            selected_session_id = selected.get("session_id")
            selected_tokens = selected.get("input_tokens")
            selected_status = selected.get("measurement_status")
            selected_worklog = selected.get("worklog_path")
            selected_generation = selected.get("generation")
            return ContextDecision(
                "resume",
                "eligible_parked_session",
                session_id=selected_session_id
                if isinstance(selected_session_id, str)
                else None,
                input_tokens=selected_tokens
                if isinstance(selected_tokens, int)
                else None,
                measurement_status=selected_status
                if isinstance(selected_status, str)
                and selected_status in MEASUREMENT_STATUSES
                else "unknown",
                worklog_path=selected_worklog
                if isinstance(selected_worklog, str)
                else None,
                generation=selected_generation
                if isinstance(selected_generation, int)
                and not isinstance(selected_generation, bool)
                else None,
            )
    return _decision_with_current(
        "fresh",
        "no_eligible_parked_session",
        input_tokens=tokens,
        measurement_status=status,
        worklog_path=worklog_path,
        generation=generation,
    )


def _new_registry(workspace_id: str) -> dict[str, object]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "workspace_id": workspace_id,
        "sessions": [],
    }


class WorkerContextRegistry:
    """Small, file-locked registry whose mutations are explicit and atomic."""

    def __init__(self, path: str | Path, *, workspace_id: str | None = None) -> None:
        self.path = Path(path).resolve()
        self.workspace_id = workspace_id or str(self.path.parents[2])

    @contextmanager
    def _locked(self) -> Iterator[dict[str, object]]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                if self.path.exists():
                    try:
                        data = json.loads(self.path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise WorkerContextError(
                            f"cannot read registry {self.path}: {exc}"
                        ) from exc
                    if not isinstance(data, dict) or not isinstance(
                        data.get("sessions"), list
                    ):
                        raise WorkerContextError(f"invalid registry {self.path}")
                else:
                    data = _new_registry(self.workspace_id)
                yield data
                write_json_atomic(path=self.path, payload=data)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def read(self) -> dict[str, object]:
        if not self.path.exists():
            return _new_registry(self.workspace_id)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorkerContextError(
                f"cannot read registry {self.path}: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), list):
            raise WorkerContextError(f"invalid registry {self.path}")
        return data

    def sessions(self) -> list[dict[str, Any]]:
        return [item for item in self.read()["sessions"] if isinstance(item, dict)]

    def register_active(self, record: dict[str, Any]) -> dict[str, Any]:
        required = ("task_key", "session_id", "agent_type")
        if any(
            not isinstance(record.get(key), str) or not record[key] for key in required
        ):
            raise WorkerContextError(
                "active registration requires task_key, session_id, agent_type"
            )
        now = utc_timestamp()
        with self._locked() as data:
            sessions = data["sessions"]
            assert isinstance(sessions, list)
            if any(
                isinstance(item, dict)
                and item.get("state") == "active"
                and item.get("task_key") == record["task_key"]
                and item.get("session_id") != record["session_id"]
                for item in sessions
            ):
                raise RegistryConflictError(
                    f"task_key {record['task_key']!r} already has an active worker"
                )
            existing = next(
                (
                    item
                    for item in sessions
                    if isinstance(item, dict)
                    and item.get("session_id") == record["session_id"]
                ),
                None,
            )
            generation = int(existing.get("generation", 0)) + 1 if existing else 1
            entry = {
                "schema_version": REGISTRY_SCHEMA_VERSION,
                "workspace_id": data.get("workspace_id", self.workspace_id),
                "task_key": record["task_key"],
                "session_id": record["session_id"],
                "agent_type": record["agent_type"],
                "model": record.get("model"),
                "state": "active",
                "pid": record.get("pid"),
                "pgid": record.get("pgid"),
                "process_starttime": record.get("process_starttime"),
                "run_id": record.get("run_id"),
                "lane": record.get("lane"),
                "stage": record.get("stage"),
                "worktree": record.get("worktree"),
                "branch": record.get("branch"),
                "input_tokens": record.get("input_tokens"),
                "measurement_status": record.get("measurement_status", "unknown"),
                "source": record.get("source"),
                "source_offset": record.get("source_offset"),
                "call_id": record.get("call_id"),
                "measured_at": record.get("measured_at"),
                "created_at": record.get("created_at", now),
                "parked_at": None,
                "last_used_at": now,
                "retired_at": None,
                "worklog_path": record.get("worklog_path"),
                "experiment_path": record.get("experiment_path"),
                "checkpoint": record.get("checkpoint"),
                "reason": record.get("reason"),
                "generation": generation,
                "pending_launch": bool(record.get("pending_launch", False)),
                "long_running_wait_requested": bool(
                    record.get("long_running_wait_requested", False)
                ),
                "job_pid": record.get("job_pid"),
                "job_process_starttime": record.get("job_process_starttime"),
                "job_log_path": record.get("job_log_path"),
            }
            if entry["measurement_status"] not in MEASUREMENT_STATUSES:
                raise WorkerContextError("invalid measurement_status")
            if existing:
                sessions[sessions.index(existing)] = entry
            else:
                sessions.append(entry)
            return dict(entry)

    def _find(self, sessions: list[object], session_id: str) -> dict[str, Any]:
        for item in sessions:
            if isinstance(item, dict) and item.get("session_id") == session_id:
                return item
        raise WorkerContextError(f"session {session_id!r} is not registered")

    def park(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        with self._locked() as data:
            sessions = data["sessions"]
            assert isinstance(sessions, list)
            entry = self._find(sessions, session_id)
            if entry.get("state") != "active":
                raise WorkerContextError("only active sessions may be parked")
            entry.update(
                state="parked",
                pid=None,
                pgid=None,
                process_starttime=None,
                parked_at=utc_timestamp(),
                last_used_at=utc_timestamp(),
                reason=reason or entry.get("reason"),
                pending_launch=False,
            )
            return dict(entry)

    def retire(self, session_id: str, *, reason: str | None = None) -> dict[str, Any]:
        with self._locked() as data:
            sessions = data["sessions"]
            assert isinstance(sessions, list)
            entry = self._find(sessions, session_id)
            if entry.get("state") != "parked":
                raise WorkerContextError("sessions must be parked before retirement")
            entry.update(
                state="retired",
                retired_at=utc_timestamp(),
                reason=reason or entry.get("reason"),
            )
            return dict(entry)

    def cleanup(
        self, settings: WorkerContextSettings, *, now: datetime | None = None
    ) -> list[str]:
        evaluated_at = now or utc_now()
        retired: list[str] = []
        with self._locked() as data:
            sessions = data["sessions"]
            assert isinstance(sessions, list)
            parked_by_key: dict[str, list[dict[str, Any]]] = {}
            for item in sessions:
                if not isinstance(item, dict) or item.get("state") != "parked":
                    continue
                parked_at = _parse_time(item.get("parked_at"))
                if parked_at is None or evaluated_at - parked_at > timedelta(
                    hours=settings.parked_ttl_hours
                ):
                    item.update(
                        state="retired",
                        retired_at=utc_timestamp(evaluated_at),
                        reason="parked_ttl_expired",
                    )
                    retired.append(str(item.get("session_id")))
                    continue
                parked_by_key.setdefault(str(item.get("task_key")), []).append(item)
            for entries in parked_by_key.values():
                entries.sort(
                    key=lambda item: (
                        _parse_time(item.get("last_used_at"))
                        or _parse_time(item.get("parked_at"))
                        or datetime.min.replace(tzinfo=timezone.utc)
                    ),
                    reverse=True,
                )
                for entry in entries[settings.max_parked_sessions :]:
                    entry.update(
                        state="retired",
                        retired_at=utc_timestamp(evaluated_at),
                        reason="parked_session_limit",
                    )
                    retired.append(str(entry.get("session_id")))
        return retired


def write_minimal_worklog(
    *,
    path: str | Path,
    task_key: str,
    goal: str,
    current_state: str,
    checkpoint: str,
    tests: str,
    error: str,
    next_step: str,
    reason: str,
    experiment_path: str | None = None,
) -> Path:
    """Atomically create the safe handoff document used when normal writing fails."""

    target = Path(path)
    lines = [
        f"key: {task_key}",
        f"goal: {goal or 'unknown'}",
        f"current state: {current_state or 'unknown'}",
        "changed files: unknown",
        f"checkpoint: {checkpoint or 'unknown'}",
        f"tests: {tests or 'unknown'}",
        "unresolved: unknown",
        f"next step: {next_step or 'unknown'}",
        "do not repeat: unknown",
        f"rollover reason: {reason or 'unknown'}",
        f"error: {error or 'unknown'}",
    ]
    if experiment_path:
        lines.append(f"experiment: {experiment_path}")
    if len(lines) > 60:
        raise WorkerContextError("minimal worklog exceeds 60 lines")
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    return target.resolve()
