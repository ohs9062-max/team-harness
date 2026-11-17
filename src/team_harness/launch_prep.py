"""Preparation and bookkeeping for th-launch."""


def cleanup():
    import json
    from pathlib import Path
    import sys

    from team_harness.worker_context import WorkerContextRegistry

    meta = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if meta.get("enabled") and meta.get("registered"):
        registry = WorkerContextRegistry(meta["registry_path"])
        entry = next(
            (
                r
                for r in registry.sessions()
                if r.get("session_id") == meta["session_id"]
            ),
            None,
        )
        if entry and entry.get("pending_launch"):
            registry.restore_after_failed_launch(
                meta["session_id"], meta.get("prior_entry")
            )


def prepare():
    import json
    import os
    from pathlib import Path
    import re
    import shutil
    import sys
    import uuid

    from team_harness.agents.process_identity import capture_starttime
    from team_harness.agents.session_capture import _read_prefix_and_tail
    from team_harness.agents.session_capture import extract_session_id
    from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
    from team_harness.config import load_worker_context_settings
    from team_harness.worker_context import ContextRequest
    from team_harness.worker_context import dispatch
    from team_harness.worker_context import measure_agent_context
    from team_harness.worker_context import parse_task_allow_paths
    from team_harness.worker_context import parse_task_metadata
    from team_harness.worker_context import registry_path_for_workspace
    from team_harness.worker_context import RegistryConflictError
    from team_harness.worker_context import should_prepare_handoff
    from team_harness.worker_context import WorkerContextError
    from team_harness.worker_context import WorkerContextRegistry
    from team_harness.worker_context import write_minimal_worklog

    workspace = Path(sys.argv[1]).resolve()
    task_dir = Path(sys.argv[2]).resolve()
    worker_type = sys.argv[3]
    model = sys.argv[4]
    worktree = (
        Path(sys.argv[5]).resolve() if Path(sys.argv[5]).exists() else Path(sys.argv[5])
    )
    out_file = Path(sys.argv[6])

    def pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            stat_path = Path(f"/proc/{pid}/stat")
            if stat_path.is_file():
                stat_text = stat_path.read_text(encoding="utf-8", errors="replace")
                if stat_text.rsplit(") ", 1)[-1].split()[0] == "Z":
                    return False
            return True
        except (OSError, IndexError):
            return False

    def read_pid(path: Path) -> int | None:
        try:
            val = path.read_text(encoding="utf-8", errors="replace").strip()
            return int(val) if val.isdigit() and int(val) > 0 else None
        except OSError:
            return None

    def capture_log_session_id(tdir: Path, agent: str, fallback: str) -> str:
        log_path = tdir / f"{agent}.log"
        template = DEFAULT_AGENT_TEMPLATES.get(agent)
        if log_path.is_file() and template is not None:
            try:
                raw = _read_prefix_and_tail(log_path, 65536)
                captured = extract_session_id(template, raw, None)
                if captured:
                    return captured
            except OSError:
                pass
        return fallback

    settings = load_worker_context_settings(workspace)
    task_md = task_dir / "task.md"
    raw_task = task_md.read_text(encoding="utf-8", errors="replace")
    lines = raw_task.splitlines()
    has_front_matter = bool(lines and lines[0].strip() == "---")

    task_key = ""
    follow_up = "normal"
    context_budget = None
    allow_paths: list[str] = []
    if has_front_matter:
        try:
            task_meta = parse_task_metadata(task_md)
            task_key = str(task_meta["task_key"])
            follow_up = str(task_meta["follow_up"])
            context_budget = task_meta.get("context_budget")
            allow_paths = parse_task_allow_paths(task_md)
        except WorkerContextError as exc:
            print(f"worker context error: {exc}", file=sys.stderr)
            sys.exit(1)

    context_active = bool(
        has_front_matter
        and task_key
        and settings.enabled
        and not settings.agent_disabled(worker_type)
    )

    job_pid = read_pid(task_dir / "job.pid")
    worker_pid_prev = read_pid(task_dir / "worker.pid")
    distinct_job_alive = (
        job_pid is not None and job_pid != worker_pid_prev and pid_alive(job_pid)
    )
    if not context_active:
        preserve_job = bool(distinct_job_alive)
        out_file.write_text(
            json.dumps(
                {
                    "enabled": False,
                    "mode": "fresh",
                    "session_id": "",
                    "preserve_job": preserve_job,
                    "handoff_text": "",
                }
            ),
            encoding="utf-8",
        )
        sys.exit(0)

    reg_path = registry_path_for_workspace(workspace)
    registry = WorkerContextRegistry(reg_path)
    sessions_root = (
        Path(os.environ.get("HOME", str(Path.home()))) / ".codex" / "sessions"
    )

    # Reap any exited active session for this task_key before dispatch; reject if still alive.
    for item in list(registry.sessions()):
        if item.get("task_key") != task_key or item.get("state") != "active":
            continue
        if item.get("pending_launch"):
            continue
        active_pid = item.get("pid")
        recorded_start = item.get("process_starttime")
        still_same_alive = False
        if isinstance(active_pid, int) and pid_alive(active_pid):
            current_start = capture_starttime(active_pid)
            if (
                recorded_start is None
                or current_start is None
                or current_start == recorded_start
            ):
                still_same_alive = True
        if not still_same_alive:
            sid = str(item.get("session_id", ""))
            ag_type = str(item.get("agent_type", worker_type))
            resolved_sid = capture_log_session_id(task_dir, ag_type, sid)
            measurement = measure_agent_context(
                agent_type=ag_type, session_id=resolved_sid, sessions_root=sessions_root
            )
            try:
                registry.update_measurement_and_park(
                    sid,
                    measurement,
                    resolved_session_id=resolved_sid,
                    reason="reaped_before_dispatch",
                )
            except WorkerContextError:
                pass

    # Long-running wait completion forces fresh analysis even when follow_up is short.
    force_fresh = (task_dir / ".th_job_completed").is_file()
    for item in list(registry.sessions()):
        if item.get("task_key") != task_key or item.get("state") != "parked":
            continue
        if item.get("reason") == "long_running_wait" or item.get(
            "long_running_wait_requested"
        ):
            force_fresh = True
            try:
                registry.retire(str(item["session_id"]), reason="long_running_wait")
            except WorkerContextError:
                pass

    effective_follow_up = (
        "normal"
        if force_fresh
        else ("short" if os.environ.get("TH_AUTO_CONTINUE") == "1" else follow_up)
    )
    try:
        decision, registry = dispatch(
            workspace=workspace,
            request=ContextRequest(
                task_key=task_key,
                agent_type=worker_type,
                follow_up=effective_follow_up,
                context_budget=context_budget,
            ),
            settings=settings,
        )
    except WorkerContextError as exc:
        print(f"worker context error: {exc}", file=sys.stderr)
        sys.exit(1)

    is_auto_continue = os.environ.get("TH_AUTO_CONTINUE") == "1"
    is_handoff = should_prepare_handoff(
        task_key=task_key,
        sessions=registry.sessions(),
        terminal=not is_auto_continue
        and ((task_dir / "DONE").is_file() or (task_dir / "FAILED").is_file()),
        context_exit_requested=is_auto_continue
        or (task_dir / "context_exit_requested.json").is_file()
        or force_fresh,
    )

    worklog_path = str((task_dir / "WORKLOG.md").resolve())
    experiment_path: str | None = None

    def find_experiment_path(tdir: Path, wl_text: str) -> Path | None:
        for m in re.finditer(r"([\w./-]*EXPERIMENT\.md)", wl_text):
            cand = Path(m.group(1))
            if not cand.is_absolute():
                for base in (tdir, tdir.parent, workspace):
                    resolved = (base / cand).resolve()
                    if resolved.is_file():
                        return resolved
            elif cand.is_file():
                return cand.resolve()
        direct = tdir / "EXPERIMENT.md"
        if direct.is_file():
            return direct.resolve()
        exp_Key = task_key.split("/", 1)[-1] if "/" in task_key else task_key
        for base in (tdir.parent, tdir, workspace):
            cand = base / "experiments" / exp_Key / "EXPERIMENT.md"
            if cand.is_file():
                return cand.resolve()
        return None

    handoff_parts: list[str] = []
    if is_handoff:
        candidate_wl = (
            Path(decision.worklog_path)
            if decision.worklog_path
            else (task_dir / "WORKLOG.md")
        )
        wl_text = ""
        wl_valid = False
        if candidate_wl.is_file():
            try:
                wl_text = candidate_wl.read_text(encoding="utf-8")
                wl_lines = wl_text.splitlines()
                if wl_text.strip() and len(wl_lines) <= 60:
                    wl_valid = True
                    worklog_path = str(candidate_wl.resolve())
            except OSError:
                wl_valid = False
        if not wl_valid:
            minimal_target = (
                task_dir / "WORKLOG.minimal.md"
                if (task_dir / "WORKLOG.md").exists()
                else task_dir / "WORKLOG.md"
            )
            exp_found = find_experiment_path(task_dir, wl_text)
            try:
                written = write_minimal_worklog(
                    path=minimal_target,
                    task_key=task_key,
                    goal=lines[0].lstrip("# ").strip() if lines else "unknown",
                    current_state="handoff_recovery",
                    checkpoint=str(worktree),
                    tests="unknown",
                    error="worklog_invalid_or_missing",
                    next_step="resume_from_checkpoint",
                    reason=decision.reason,
                    experiment_path=str(exp_found) if exp_found else None,
                )
                worklog_path = str(written)
                wl_text = written.read_text(encoding="utf-8")
            except (OSError, WorkerContextError) as exc:
                print(f"blocked: cannot write minimal worklog: {exc}", file=sys.stderr)
                sys.exit(1)
        exp_doc = find_experiment_path(task_dir, wl_text)
        if exp_doc is not None:
            experiment_path = str(exp_doc)
        if decision.action == "fresh":
            handoff_parts.append(
                f"## Handoff WORKLOG ({worklog_path})\n{wl_text.strip()}"
            )
            if exp_doc is not None:
                try:
                    exp_lines = exp_doc.read_text(encoding="utf-8").splitlines()
                    if exp_lines and len(exp_lines) <= 80:
                        handoff_parts.append(
                            f"## Handoff EXPERIMENT ({exp_doc})\n"
                            + "\n".join(exp_lines)
                        )
                    elif exp_lines:
                        handoff_parts.append(
                            f"## Handoff EXPERIMENT\nPrior experiment is at {exp_doc}; it exceeds the 80-line handoff limit."
                        )
                except OSError:
                    pass
    else:
        exp_doc = find_experiment_path(task_dir, raw_task)
        if exp_doc is not None:
            experiment_path = str(exp_doc)

    if os.environ.get("TH_FAIL_LAUNCH") == "1" or shutil.which(worker_type) is None:
        print(f"worker launch failed: {worker_type} unavailable", file=sys.stderr)
        sys.exit(1)

    spawn_mode = (
        "resume" if decision.action == "resume" and decision.session_id else "fresh"
    )
    session_id = (
        decision.session_id
        if spawn_mode == "resume" and decision.session_id
        else str(uuid.uuid4())
    )
    prior_entry = next(
        (
            dict(item)
            for item in registry.sessions()
            if item.get("session_id") == session_id
        ),
        None,
    )

    preserve_job = bool(
        distinct_job_alive
        or (job_pid is not None and job_pid != worker_pid_prev and is_handoff)
    )
    out_file.write_text(
        json.dumps(
            {
                "enabled": True,
                "registry_path": str(reg_path),
                "workspace": str(workspace),
                "task_key": task_key,
                "follow_up": follow_up,
                "context_budget": context_budget,
                "allow_paths": allow_paths,
                "agent_type": worker_type,
                "mode": spawn_mode,
                "session_id": session_id,
                "model": model,
                "worktree": str(worktree),
                "worklog_path": worklog_path,
                "experiment_path": experiment_path,
                "prior_entry": prior_entry,
                "preserve_job": preserve_job,
                "handoff_text": "\n\n".join(handoff_parts),
            }
        ),
        encoding="utf-8",
    )
    try:
        registry.register_active(
            {
                "task_key": task_key,
                "session_id": session_id,
                "agent_type": worker_type,
                "model": model,
                "worktree": str(worktree),
                "worklog_path": worklog_path,
                "experiment_path": experiment_path,
                "pending_launch": True,
            }
        )
    except (RegistryConflictError, WorkerContextError) as exc:
        print(f"worker context conflict: {exc}", file=sys.stderr)
        sys.exit(1)

    # Only our successful registration may be rolled back. A conflicting launch
    # can point at the same resume session but must never restore that entry.
    try:
        prepared = json.loads(out_file.read_text(encoding="utf-8"))
        prepared["registered"] = True
        out_file.write_text(json.dumps(prepared), encoding="utf-8")
    except (OSError, ValueError):
        registry.restore_after_failed_launch(session_id, prior_entry)
        raise


def meta():
    import json
    import sys

    data = json.loads(open(sys.argv[1], encoding="utf-8").read())
    print("1" if data.get("enabled") else "0")
    print(data.get("mode", "fresh"))
    print(data.get("session_id", ""))
    print("1" if data.get("preserve_job") else "0")


def handoff():
    import json
    import sys

    data = json.loads(open(sys.argv[1], encoding="utf-8").read())
    print(data.get("handoff_text", ""), end="")


def allow_paths():
    import json
    import sys

    data = json.loads(open(sys.argv[1], encoding="utf-8").read())
    print(json.dumps(data.get("allow_paths", [])))


def bind():
    import json
    import os
    from pathlib import Path
    import sys

    from team_harness.agents.process_identity import capture_starttime
    from team_harness.config import load_worker_context_settings
    from team_harness.worker_context import WorkerContextRegistry

    meta = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    task_dir = Path(sys.argv[2]).resolve()
    registry = WorkerContextRegistry(meta["registry_path"])
    session_id = meta["session_id"]
    prior_entry = meta.get("prior_entry")

    try:
        pid_str = (task_dir / "worker.pid").read_text(encoding="utf-8").strip()
        worker_pid = int(pid_str)
    except (OSError, ValueError):
        registry.restore_after_failed_launch(session_id, prior_entry)
        sys.exit(1)

    starttime = capture_starttime(worker_pid)
    try:
        pgid = os.getpgid(worker_pid)
    except OSError:
        pgid = worker_pid

    registry.register_active(
        {
            "task_key": meta["task_key"],
            "session_id": session_id,
            "agent_type": meta["agent_type"],
            "model": meta["model"],
            "pid": worker_pid,
            "pgid": pgid,
            "process_starttime": starttime,
            "worktree": meta["worktree"],
            "worklog_path": meta["worklog_path"],
            "experiment_path": meta.get("experiment_path"),
            "pending_launch": False,
        }
    )
    settings = load_worker_context_settings(Path(meta["workspace"]))
    registry.cleanup(settings)
    (task_dir / ".th_context.json").write_text(
        json.dumps(
            {
                "registry_path": meta["registry_path"],
                "workspace": meta["workspace"],
                "task_key": meta["task_key"],
                "follow_up": meta["follow_up"],
                "context_budget": meta.get("context_budget"),
                "session_id": session_id,
                "agent_type": meta["agent_type"],
                "worklog_path": meta["worklog_path"],
                "experiment_path": meta.get("experiment_path"),
                "parked": False,
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def usage():
    import json
    import sys

    at, task, attempt, worker, model, effort, owner, override, rollover = sys.argv[1:]
    print(
        '{"v":1,"at":"%s","task":%s,"attempt":%s,"worker":%s,"model":%s,"effort":%s,"owner":%s,"override":%s,"rollover":%s}'
        % (
            at,
            json.dumps(task),
            attempt,
            json.dumps(worker),
            json.dumps(model),
            json.dumps(effort),
            json.dumps(owner),
            json.dumps(override if override else None),
            rollover,
        )
    )


if __name__ == "__main__":
    import sys

    command = sys.argv.pop(1)
    {
        "cleanup": cleanup,
        "prepare": prepare,
        "meta": meta,
        "handoff": handoff,
        "allow-paths": allow_paths,
        "bind": bind,
        "usage": usage,
    }[command]()
