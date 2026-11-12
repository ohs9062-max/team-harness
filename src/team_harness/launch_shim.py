"""Best-effort observation before direct CLI exec; never enforce decisions."""

from __future__ import annotations

from datetime import datetime
from datetime import UTC
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys


def _option(args: list[str], *names: str) -> str:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg[len(name) + 1 :]
    return ""


def _prompt(tool: str, args: list[str]) -> str | None:
    if tool == "agy":
        return _option(args, "-p", "--prompt") or None
    # Only inspect an unambiguous final positional prompt. Option values and
    # stdin are never read or mistaken for a brief.
    if not args or args[-1] in {"-", "exec", "resume"} or args[-1].startswith("-"):
        return None
    options_with_values = {
        "-m",
        "--model",
        "-c",
        "--config",
        "-C",
        "--cd",
        "-o",
        "--output-last-message",
        "--sandbox",
        "-s",
        "--profile",
        "-p",
        "--output-schema",
        "--image",
        "-i",
        "--add-dir",
        "--color",
    }
    if len(args) >= 2 and args[-2] in options_with_values:
        return None
    return args[-1]


def _effort(args: list[str]) -> str:
    direct = _option(args, "--effort")
    if direct:
        return direct
    for i, arg in enumerate(args):
        if arg in {"-c", "--config"} and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--config="):
            value = arg[len("--config=") :]
        else:
            continue
        key, sep, value = value.partition("=")
        if sep and key.strip() == "model_reasoning_effort":
            return value.strip().strip("\"'")
    return ""


def _record(tool: str, args: list[str], depth: int, pid: int | None = None) -> None:
    from team_harness import launch_policy
    from team_harness import resource_slots
    from team_harness.brief_check import check_brief_text
    from team_harness.worker_digest import quota_lock_active
    from team_harness.workspace_archive import watch_dir

    wd = watch_dir()
    task = Path(
        os.environ.get("TH_WORKER_OUTPUT_DIR")
        or os.environ.get("TH_JOB_TASK")
        or os.getcwd()
    )
    model = _option(args, "-m", "--model")
    effort = _effort(args)
    try:
        rows = json.loads((wd / "admission.json").read_text())
        rows = {key: row for key, row in rows.items() if resource_slots.active(row)}
        workers = resource_slots._workers(
            rows, task, resource_slots.worker_family(tool), wd
        )
    except FileNotFoundError:
        workers = launch_policy._active_workers(task, tool, wd)
    violations = launch_policy.check(
        task, tool, model, effort, wd, active_workers=workers
    )
    verdicts: dict = {
        "policy": [v._asdict() for v in violations],
        "warnings": launch_policy.check_warnings(tool),
    }
    quota = os.environ.get("TH_IGNORE_QUOTA_LOCK") != "1" and quota_lock_active(
        wd / f"quota_lock.{tool}"
    )
    prompt = _prompt(tool, args)
    findings = check_brief_text(prompt, task) if prompt is not None else []
    verdicts.update(
        work_location=os.getcwd(),
        quota_lock=quota,
        brief=[
            {"level": f.level, "rule": f.rule, "line": f.line, "message": f.message}
            for f in findings
        ]
        if prompt is not None
        else "unavailable_stdin",
        prompt_hash=hashlib.sha256(prompt.encode()).hexdigest()
        if prompt is not None
        else None,
    )
    max_depth = int(os.environ.get("TH_SHIM_MAX_DEPTH", "4"))
    verdicts["depth_exceeded"] = depth > max_depth
    wd.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(wd / "th.db", timeout=0) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS shim_decisions(at TEXT, pid INTEGER, session_id TEXT, workspace TEXT, worker_task TEXT, tool TEXT, model TEXT, effort TEXT, depth INTEGER, verdicts_json TEXT, would_block INTEGER, mode TEXT)"
        )
        workspace = os.environ.get("TH_WORKSPACE") or os.getcwd()
        previous = db.execute(
            "SELECT verdicts_json, worker_task FROM shim_decisions WHERE workspace=? AND at>=?",
            (
                workspace,
                datetime.fromtimestamp(
                    datetime.now(UTC).timestamp() - 86400, UTC
                ).isoformat(),
            ),
        ).fetchall()
        same = [
            row
            for row in previous
            if prompt is not None
            and json.loads(row[0]).get("prompt_hash") == verdicts["prompt_hash"]
            and json.loads(row[0]).get("work_location") == verdicts["work_location"]
        ]
        verdicts["recent_repeats"] = len(same)
        # Only indexed task failures can be attributed; direct exec exit status
        # itself cannot be observed after replacing this process.
        task_paths = {row[1] for row in same if row[1]}
        try:
            verdicts["recent_failures"] = (
                sum(
                    db.execute(
                        "SELECT count(DISTINCT attempt) FROM failures WHERE task=? AND at>=?",
                        (
                            path,
                            datetime.fromtimestamp(
                                datetime.now(UTC).timestamp() - 86400, UTC
                            ).isoformat(),
                        ),
                    ).fetchone()[0]
                    for path in task_paths
                )
                if task_paths
                else None
            )
        except sqlite3.OperationalError:
            verdicts["recent_failures"] = None
        blocked = (
            any(v.code != "budget" for v in violations)
            or quota
            or verdicts["depth_exceeded"]
            or (
                verdicts["recent_failures"] is not None
                and verdicts["recent_failures"] >= 2
            )
            or any(f.level == "error" for f in findings)
        )
        db.execute(
            "INSERT INTO shim_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                datetime.now(UTC).isoformat(),
                pid if pid is not None else os.getpid(),
                os.environ.get("CLAUDE_CODE_SESSION_ID"),
                workspace,
                os.environ.get("TH_WORKER_OUTPUT_DIR") or os.environ.get("TH_JOB_TASK"),
                tool,
                model,
                effort,
                depth,
                json.dumps(verdicts, ensure_ascii=False),
                int(blocked),
                "observe",
            ),
        )


def main(tool: str) -> None:
    shim_dir = Path(sys.argv[0]).absolute().parent.resolve()
    path = os.pathsep.join(
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if Path(p or ".").resolve() != shim_dir
        and (Path(p or ".") / tool).resolve() != Path(sys.argv[0]).resolve()
    )
    real = shutil.which(tool, path=path)
    if real is None:
        print(f"{tool}: command not found", file=sys.stderr)
        raise SystemExit(127)
    args = sys.argv[1:]
    noninteractive = (
        (
            "exec" in args
            and ("resume" not in args or args.index("exec") < args.index("resume"))
        )
        if tool == "codex"
        else bool(_option(args, "-p", "--prompt")) or not sys.stdin.isatty()
    )
    if os.environ.get("TH_SHIM") != "0" and noninteractive:
        try:
            from team_harness.proc_observer import has_ancestor_command

            if not has_ancestor_command("th-launch.sh"):
                depth = int(os.environ.get("TH_SHIM_DEPTH", "0")) + 1
                os.environ["TH_SHIM_DEPTH"] = str(depth)
                _record(tool, args, depth)
        except Exception:
            pass
    os.execv(real, [real, *args])


def observe_child() -> None:
    """Observe in an expendable subprocess, without consuming caller input."""
    tool, tty, *args = sys.argv[1:]
    noninteractive = (
        (
            "exec" in args
            and ("resume" not in args or args.index("exec") < args.index("resume"))
        )
        if tool == "codex"
        else bool(_option(args, "-p", "--prompt")) or tty == "0"
    )
    if not noninteractive:
        return
    from team_harness.proc_observer import has_ancestor_command

    if has_ancestor_command("th-launch.sh"):
        return
    depth = int(os.environ.get("TH_SHIM_DEPTH", "0")) + 1
    print(depth, flush=True)
    _record(tool, args, depth, int(os.environ["TH_SHIM_PARENT_PID"]))
