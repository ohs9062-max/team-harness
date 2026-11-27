"""Shared, best-effort execution stages and their append-only event ledger."""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Callable
import uuid


@dataclass
class ExecEvent:
    tool: str = ""
    role: str = ""
    session: str = ""
    cwd: str = ""
    task: str = ""
    owner: str = ""
    argv: list[str] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    pid: int | None = None
    process_start: str = ""
    exit_code: int | None = None
    output_paths: list[str] = field(default_factory=list)
    result_paths: list[str] = field(default_factory=list)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    depth: int = 0
    ended_at: str = ""
    route: str = ""
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    context: dict = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


@dataclass
class StageResult:
    name: str
    code: int
    message: str = ""
    block: bool = False


Stage = Callable[[ExecEvent], StageResult]
_pre: list[tuple[str, Stage]] = []
_post: list[tuple[str, Stage]] = []


def register_pre(name: str, fn: Stage) -> None:
    _pre.append((name, fn))


def register_post(name: str, fn: Stage) -> None:
    _post.append((name, fn))


def _run(
    event: ExecEvent, stages: list[tuple[str, Stage]], phase: str
) -> list[StageResult]:
    from team_harness.workspace_archive import watch_dir

    results = []
    for name, fn in tuple(stages):
        try:
            result = fn(event)
        except Exception as exc:
            result = StageResult(name, 1, f"{type(exc).__name__}: {exc}")
        results.append(result)
        try:
            wd = watch_dir()
            wd.mkdir(parents=True, exist_ok=True)
            data = asdict(event)
            data["argv"] = []
            data["message"] = ""
            data["warnings"] = []
            data["context"] = {"prompt_hash": event.context.get("prompt_hash")}
            stage_data = asdict(result)
            # Stage messages may quote arbitrary parts of prompts or other input.
            stage_data["message"] = "[redacted]" if result.message else ""
            with (wd / "exec_pipeline.jsonl").open("a", encoding="utf-8") as ledger:
                ledger.write(
                    json.dumps(
                        {"phase": phase, "event": data, "stage": stage_data},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        except (OSError, TypeError, ValueError):
            pass
    return results


def run_pre(event: ExecEvent) -> list[StageResult]:
    return _run(event, _pre, "pre")


def run_post(event: ExecEvent) -> list[StageResult]:
    return _run(event, _post, "post")


def find_pre_event(pid: int, start: str) -> ExecEvent | None:
    """Find the originating event only when both process identity parts match."""
    from team_harness.workspace_archive import watch_dir

    if not start:
        return None
    found = None
    try:
        with (watch_dir() / "exec_pipeline.jsonl").open(encoding="utf-8") as ledger:
            for line in ledger:
                try:
                    item = json.loads(line)
                    data = item.get("event", {})
                    if (
                        item.get("phase") == "pre"
                        and data.get("pid") == pid
                        and data.get("process_start") == start
                    ):
                        found = ExecEvent(**data)
                except (ValueError, TypeError, AttributeError):
                    continue
    except OSError:
        pass
    return found


def _policy(event: ExecEvent) -> StageResult:
    from team_harness import launch_policy
    from team_harness import maintenance
    from team_harness import resource_slots
    from team_harness.agents.process_identity import capture_starttime
    from team_harness.launch_shim import _effort
    from team_harness.launch_shim import _option
    from team_harness.workspace_archive import watch_dir

    wd = watch_dir()
    task = Path(event.task or event.cwd or os.getcwd()).resolve()
    cwd = Path(event.cwd or os.getcwd()).resolve()
    if event.route not in {"th_launch", "th_job_start"} and (cwd / "task.md").is_file():
        task = cwd
    owner = next(
        (
            p
            for base in (task, cwd)
            for p in (base, *base.parents)
            if (p / ".git").exists()
        ),
        cwd,
    )
    event.task, event.owner = str(task), str(owner)
    model, effort = _option(event.argv, "-m", "--model"), _effort(event.argv)
    event.context.update(model=model, effort=effort)
    maintenance.start(owner=owner)
    with resource_slots.ledger(wd) as rows:
        for key in list(rows):
            if not resource_slots.active(rows[key]):
                del rows[key]
        workers = resource_slots._workers(
            rows, task, resource_slots.worker_family(event.tool), wd
        )
        violations = launch_policy.check(
            task, event.tool, model, effort, wd, active_workers=workers
        )
        owner_pid = event.pid if event.pid is not None else os.getpid()
        rows[uuid.uuid4().hex] = {
            "pid": owner_pid,
            "start": capture_starttime(owner_pid),
            "worker": resource_slots.worker_family(event.tool),
            "task": str(task),
            "bound": False,
        }
    event.context["policy"] = [v._asdict() for v in violations]
    event.context["policy_warnings"] = launch_policy.check_warnings(event.tool)
    return StageResult("execution_policy", 0)


def _quota(event: ExecEvent) -> StageResult:
    from team_harness.quota_lock import active
    from team_harness.workspace_archive import watch_dir

    event.context["quota_lock"] = os.environ.get(
        "TH_IGNORE_QUOTA_LOCK"
    ) != "1" and active(watch_dir() / f"quota_lock.{event.tool}")
    return StageResult("quota_lock", 0)


def _brief(event: ExecEvent) -> StageResult:
    from team_harness.brief_check import check_brief_text
    from team_harness.launch_shim import _prompt

    prompt = _prompt(event.tool, event.argv)
    findings = check_brief_text(prompt, Path(event.task)) if prompt is not None else []
    event.context["brief"] = (
        [
            {"level": f.level, "rule": f.rule, "line": f.line, "message": f.message}
            for f in findings
        ]
        if prompt is not None
        else "unavailable_stdin"
    )
    event.context["prompt_hash"] = (
        hashlib.sha256(prompt.encode()).hexdigest() if prompt is not None else None
    )
    return StageResult("brief_check", 0)


def _repeat(event: ExecEvent) -> StageResult:
    from team_harness.workspace_archive import watch_dir

    ctx = event.context
    wd = watch_dir()
    max_depth = int(os.environ.get("TH_SHIM_MAX_DEPTH", "4"))
    verdicts = {
        "policy": ctx.get("policy", []),
        "warnings": ctx.get("policy_warnings", []),
        "work_location": event.cwd,
        "quota_lock": ctx.get("quota_lock", False),
        "brief": ctx.get("brief", "unavailable_stdin"),
        "prompt_hash": ctx.get("prompt_hash"),
        "depth_exceeded": event.depth > max_depth,
    }
    wd.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(wd / "th.db", timeout=0.1) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS shim_decisions(at TEXT, pid INTEGER, session_id TEXT, workspace TEXT, worker_task TEXT, tool TEXT, model TEXT, effort TEXT, depth INTEGER, verdicts_json TEXT, would_block INTEGER, mode TEXT)"
        )
        workspace = os.environ.get("TH_WORKSPACE") or event.cwd
        since = datetime.fromtimestamp(
            datetime.now(UTC).timestamp() - 86400, UTC
        ).isoformat()
        previous = db.execute(
            "SELECT verdicts_json, worker_task FROM shim_decisions WHERE workspace=? AND at>=?",
            (workspace, since),
        ).fetchall()
        same = [
            row
            for row in previous
            if verdicts["prompt_hash"] is not None
            and json.loads(row[0]).get("prompt_hash") == verdicts["prompt_hash"]
            and json.loads(row[0]).get("work_location") == verdicts["work_location"]
        ]
        verdicts["recent_repeats"] = len(same)
        task_paths = {row[1] for row in same if row[1]}
        try:
            verdicts["recent_failures"] = (
                sum(
                    db.execute(
                        "SELECT count(DISTINCT attempt) FROM failures WHERE task=? AND at>=?",
                        (path, since),
                    ).fetchone()[0]
                    for path in task_paths
                )
                if task_paths
                else None
            )
        except sqlite3.OperationalError:
            verdicts["recent_failures"] = None
        policy = verdicts["policy"]
        findings = verdicts["brief"] if isinstance(verdicts["brief"], list) else []
        blocked = (
            any(v["code"] != "budget" for v in policy)
            or verdicts["quota_lock"]
            or verdicts["depth_exceeded"]
            or (
                verdicts["recent_failures"] is not None
                and verdicts["recent_failures"] >= 2
            )
            or any(f["level"] == "error" for f in findings)
        )
        task = Path(event.task)
        db.execute(
            "INSERT INTO shim_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(),
                event.pid if event.pid is not None else os.getpid(),
                os.environ.get("CLAUDE_CODE_SESSION_ID"),
                workspace,
                str(task)
                if (task / "task.md").is_file()
                else (
                    os.environ.get("TH_WORKER_OUTPUT_DIR")
                    or os.environ.get("TH_JOB_TASK")
                ),
                event.tool,
                ctx.get("model", ""),
                ctx.get("effort", ""),
                event.depth,
                json.dumps(verdicts, ensure_ascii=False),
                int(blocked),
                "observe",
            ),
        )
    warnings = [
        f"{f['level']} {f['rule']}:{f['line']}: {f['message']}" for f in findings
    ]
    warnings.extend(
        v["message"].replace("거부", "상한 초과")
        for v in policy
        if v["code"] in {"parallel", "budget"}
    )
    if verdicts["quota_lock"]:
        warnings.append(f"{event.tool}: quota lock active")
    if verdicts["depth_exceeded"]:
        warnings.append(f"실행 깊이 {event.depth}가 상한 {max_depth}을 초과했습니다")
    if verdicts["recent_failures"] is not None and verdicts["recent_failures"] >= 2:
        warnings.append(f"같은 작업의 최근 실패 {verdicts['recent_failures']}회")
    event.warnings = [" ".join(w.splitlines()) for w in warnings]
    return StageResult("repeat_warning", 0, "\n".join(event.warnings))


def _observed_verify(event: ExecEvent) -> StageResult:
    if event.route in {"path_shim", "claude_hook", "th_launch", "th_job_start"}:
        return StageResult("observed_verify", 0, "registered task handled by watchd")

    from team_harness.observed_verify import launch

    launched = launch(
        Path(event.task), event.ended_at, event.owner, event.route, event.message
    )
    return StageResult(
        "observed_verify",
        0 if launched else 1,
        "handled; verification may be pending or unavailable"
        if launched
        else "unavailable",
    )


register_pre("execution_policy", _policy)
register_pre("quota_lock", _quota)
register_pre("brief_check", _brief)
register_pre("repeat_warning", _repeat)
register_post("observed_verify", _observed_verify)
