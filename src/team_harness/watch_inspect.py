"""th-watch inspection: context rollover, parking and detached-job state for one task folder.

Run as ``python -m team_harness.watch_inspect <task_dir>``; prints one
``event<TAB>distinct_job_alive<TAB>detail`` line consumed by scripts/th-watch-lib.sh.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.session_capture import _read_prefix_and_tail
from team_harness.agents.session_capture import extract_session_id
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.config import load_worker_context_settings
from team_harness.tracking.persistence import write_json_atomic
from team_harness.worker_context import ContextRequest
from team_harness.worker_context import decide
from team_harness.worker_context import measure_agent_context
from team_harness.worker_context import measure_codex_rollout
from team_harness.worker_context import parse_task_front_matter
from team_harness.worker_context import parse_task_metadata
from team_harness.worker_context import registry_path_for_workspace
from team_harness.worker_context import utc_timestamp
from team_harness.worker_context import WorkerContextError
from team_harness.worker_context import WorkerContextRegistry
from team_harness.worker_context import write_minimal_worklog
from team_harness.worker_digest import pid_alive


def read_pid(path: Path) -> int | None:
    try:
        val = path.read_text(encoding="utf-8", errors="replace").strip()
        return int(val) if val.isdigit() and int(val) > 0 else None
    except OSError:
        return None


def get_pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def main(task: Path) -> None:
    worker_pid = read_pid(task / "worker.pid")
    job_pid = read_pid(task / "job.pid")
    worker_alive = pid_alive(worker_pid) if worker_pid is not None else False
    # A job.pid identical to worker.pid is the worker itself, never a detached long job.
    has_distinct_job = job_pid is not None and job_pid != worker_pid
    job_alive = (
        pid_alive(job_pid) if has_distinct_job and job_pid is not None else False
    )

    seen_job_file = task / ".th_distinct_job_seen"
    if has_distinct_job and job_pid is not None:
        if job_alive and not seen_job_file.is_file():
            write_json_atomic(
                path=seen_job_file,
                payload={
                    "job_pid": job_pid,
                    "job_starttime": capture_starttime(job_pid),
                    "recorded_at": utc_timestamp(),
                },
            )

    def load_ctx_meta() -> dict | None:
        ctx_file = task / ".th_context.json"
        if ctx_file.is_file():
            try:
                return json.loads(ctx_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
        owner_file = task / ".th_owner"
        task_md = task / "task.md"
        if owner_file.is_file() and task_md.is_file():
            try:
                ws = Path(owner_file.read_text(encoding="utf-8").strip()).resolve()
                reg_path = registry_path_for_workspace(ws)
                if reg_path.is_file():
                    tkey, fup = parse_task_front_matter(task_md)
                    reg = WorkerContextRegistry(reg_path)
                    for item in reversed(reg.sessions()):
                        if (
                            item.get("task_key") == tkey
                            and item.get("state") == "active"
                        ):
                            return {
                                "registry_path": str(reg_path),
                                "workspace": str(ws),
                                "task_key": tkey,
                                "follow_up": fup,
                                "session_id": str(item.get("session_id")),
                                "agent_type": str(item.get("agent_type", "codex")),
                                "worklog_path": item.get("worklog_path")
                                or str(task / "WORKLOG.md"),
                                "parked": False,
                            }
            except Exception:
                pass
        return None

    def park_worker_if_needed(reason_override: str | None = None) -> None:
        meta = load_ctx_meta()
        if not meta or meta.get("parked"):
            return
        reg_path = Path(meta["registry_path"])
        if not reg_path.is_file():
            return
        agent_type = str(meta.get("agent_type", "codex"))
        sid = str(meta.get("session_id", ""))
        resolved_sid = sid
        log_path = task / f"{agent_type}.log"
        template = DEFAULT_AGENT_TEMPLATES.get(agent_type)
        if log_path.is_file() and template is not None:
            try:
                raw = _read_prefix_and_tail(log_path, 65536)
                captured = extract_session_id(template, raw, None)
                if captured:
                    resolved_sid = captured
            except OSError:
                pass
        sessions_root = (
            Path(os.environ.get("HOME", str(Path.home()))) / ".codex" / "sessions"
        )
        measurement = measure_agent_context(
            agent_type=agent_type, session_id=resolved_sid, sessions_root=sessions_root
        )
        reason = reason_override
        if reason is None:
            if (task / "context_exit_requested.json").is_file() or (
                has_distinct_job and job_alive
            ):
                reason = "long_running_wait"
            elif (task / "FAILED").is_file():
                reason = "failed"
            elif (task / "DONE").is_file():
                reason = "completed"
        registry = WorkerContextRegistry(reg_path)
        try:
            registry.update_measurement_and_park(
                sid, measurement, resolved_session_id=resolved_sid, reason=reason
            )
        except WorkerContextError:
            pass
        meta["session_id"] = resolved_sid
        meta["parked"] = True
        if (task / ".th_context.json").is_file():
            write_json_atomic(path=task / ".th_context.json", payload=meta)

    # The watcher enforces the cap even when a worker never invokes the CLI
    # itself.  Codex's rollout input_tokens is the current context size; its
    # separate cumulative "tokens used" figure is deliberately not a limit.
    if (
        worker_alive
        and not (task / "DONE").is_file()
        and not (task / "FAILED").is_file()
    ):
        meta = load_ctx_meta()
        request_file = task / "context_exit_requested.json"
        if (
            meta
            and str(meta.get("agent_type")) == "codex"
            and meta.get("session_id")
            and not request_file.is_file()
        ):
            try:
                metadata = parse_task_metadata(task / "task.md")
                workspace = Path(str(meta.get("workspace"))).resolve()
                measurement = measure_codex_rollout(session_id=str(meta["session_id"]))
                decision = decide(
                    ContextRequest(
                        task_key=str(metadata["task_key"]),
                        agent_type="codex",
                        follow_up=str(metadata["follow_up"]),
                        phase="boundary",
                        context_budget=metadata.get("context_budget"),
                    ),
                    {
                        "input_tokens": measurement.input_tokens,
                        "measurement_status": measurement.measurement_status,
                    },
                    load_worker_context_settings(workspace),
                )
                if (
                    decision.action == "rollover"
                    and decision.reason == "context_cap_exceeded"
                ):
                    write_json_atomic(
                        path=request_file,
                        payload={
                            "action": "rollover",
                            "reason": decision.reason,
                            "input_tokens": measurement.input_tokens,
                            "measurement_status": measurement.measurement_status,
                            "basis": "current_input_context_not_cumulative_tokens_used",
                            "requested_at": utc_timestamp(),
                            "requested_at_epoch": int(time.time()),
                            "worker_pid": worker_pid,
                        },
                    )
            except (OSError, WorkerContextError, ValueError):
                pass

    if not worker_alive:
        park_worker_if_needed()

    def artifact_signature() -> str:
        skip_names = {
            "digest.md",
            "VERIFIED",
            "VERIFY_FAILED",
            "task.md",
            "verify.sh",
            "context_exit_requested.json",
            "codex.log",
            "claude.log",
            "agy.log",
            "worker.log",
            "job.log",
        }
        entries: list[str] = []
        for root, dirs, files in os.walk(task):
            rel_root = Path(root).relative_to(task)
            if len(rel_root.parts) > 3:
                dirs[:] = []
                continue
            for fname in sorted(files):
                if (
                    fname.startswith(".th_")
                    or fname.startswith("verify_result.")
                    or fname.startswith("workers")
                    or fname.endswith(".pid")
                    or fname in skip_names
                ):
                    continue
                fpath = Path(root) / fname
                try:
                    st = fpath.stat()
                except OSError:
                    continue
                rel = str(fpath.relative_to(task))
                if fname in {
                    "STATUS",
                    "WORKLOG.md",
                    "WORKLOG.minimal.md",
                    "EXPERIMENT.md",
                }:
                    try:
                        digest = hashlib.sha1(fpath.read_bytes()).hexdigest()[:12]
                    except OSError:
                        digest = str(st.st_mtime_ns)
                    entries.append(f"{rel}:{digest}")
                else:
                    entries.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
        return "|".join(entries)

    _SLEEP_CMD_RE = re.compile(
        r"(?:^|[;&|()\s])sleep\s+[0-9]+(?:\.[0-9]+)?(?:\s|$|[;&|)])"
    )
    _STATUS_ONLY_BINS = {
        "sleep",
        "ps",
        "kill",
        "pgrep",
        "cat",
        "head",
        "tail",
        "ls",
        "stat",
        "wc",
        "grep",
        "egrep",
        "fgrep",
        "echo",
        "printf",
        "date",
        "true",
        "test",
        "[",
        "[[",
        "ths",
        "th-status",
        "th-status.sh",
    }

    def classify_tool_command(cmd: str) -> str:
        stripped = cmd.strip()
        if not stripped:
            return "IGNORE"
        # Unwrap outer bash -lc / sh -c quotes if present.
        m = re.match(
            r"^(?:/usr/bin/env\s+)?(?:/bin/)?(?:ba)?sh\s+-[l]?c\s+(['\"])(.*)\1$",
            stripped,
            re.DOTALL,
        )
        if m:
            stripped = m.group(2).strip()
        segments = [
            seg.strip()
            for seg in re.split(r"(?:&&|\|\||[;|\n])+", stripped)
            if seg.strip()
        ]
        if not segments:
            return "IGNORE"
        has_sleep = False
        all_status_or_sleep = True
        for seg in segments:
            tokens = seg.split()
            while tokens and ("=" in tokens[0] and not tokens[0].startswith("-")):
                tokens.pop(0)
            if not tokens:
                continue
            exe = os.path.basename(tokens[0].lstrip("("))
            if (
                exe == "sleep"
                and len(tokens) >= 2
                and re.match(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$", tokens[1].rstrip(")"))
            ):
                has_sleep = True
                continue
            if exe == "kill" and any(tok == "-0" for tok in tokens[1:]):
                continue
            if exe in _STATUS_ONLY_BINS and exe != "kill":
                if any(op in seg for op in (" >", ">>", "tee ")):
                    all_status_or_sleep = False
                continue
            all_status_or_sleep = False
        if has_sleep and all_status_or_sleep:
            return "SLEEP_POLL"
        if all_status_or_sleep:
            return "STATUS_CHECK"
        return "WORK"

    def extract_tool_commands_from_json(event: object, seen_ids: set[str]) -> list[str]:
        if not isinstance(event, dict):
            return []
        cmds: list[str] = []
        ev_type = str(event.get("type", ""))
        item = event.get("item")
        if isinstance(item, dict):
            item_id = str(item.get("id") or "")
            item_type = str(item.get("type") or "")
            if item_type in {
                "command_execution",
                "exec_command",
                "tool_call",
                "tool_use",
                "function_call",
            }:
                if item_id:
                    if item_id in seen_ids:
                        return []
                    seen_ids.add(item_id)
                cmd = item.get("command") or item.get("cmd")
                if isinstance(cmd, str) and cmd.strip():
                    cmds.append(cmd)
        if ev_type in {
            "command_execution",
            "exec_command",
            "tool_call",
            "tool_use",
            "function_call",
        }:
            call_id = str(event.get("call_id") or event.get("id") or "")
            if call_id:
                if call_id in seen_ids:
                    return []
                seen_ids.add(call_id)
            cmd = event.get("command") or event.get("cmd")
            if not isinstance(cmd, str):
                inp = event.get("input") or event.get("arguments")
                if isinstance(inp, str):
                    try:
                        inp = json.loads(inp)
                    except json.JSONDecodeError:
                        inp = None
                if isinstance(inp, dict):
                    cmd = inp.get("command") or inp.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                cmds.append(cmd)
        msg = event.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                cmds.extend(extract_tool_commands_from_json(part, seen_ids))
        payload = event.get("payload")
        if isinstance(payload, dict):
            cmds.extend(extract_tool_commands_from_json(payload, seen_ids))
        return cmds

    def extract_worker_tool_events(tdir: Path) -> tuple[str, list[str]]:
        if (
            (tdir / "agy.log").is_file()
            and not (tdir / "codex.log").is_file()
            and not (tdir / "claude.log").is_file()
        ):
            return "unsupported", []
        for name in ("codex.log", "claude.log", "worker.log"):
            log_path = tdir / name
            if not log_path.is_file():
                continue
            try:
                lines = log_path.read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                continue
            cmds: list[str] = []
            seen_ids: set[str] = set()
            saw_structured = False
            idx = 0
            while idx < len(lines):
                line = lines[idx].strip()
                if line.startswith("{") and line.endswith("}"):
                    try:
                        obj = json.loads(line)
                        saw_structured = True
                        cmds.extend(extract_tool_commands_from_json(obj, seen_ids))
                        idx += 1
                        continue
                    except json.JSONDecodeError:
                        pass
                if line == "exec" and idx + 1 < len(lines):
                    nxt = lines[idx + 1].strip()
                    m = re.match(
                        r"^(/bin/(?:ba)?sh\s+-[l]?c\s+.+?)(?:\s+in\s+\S+)?$", nxt
                    )
                    if m:
                        saw_structured = True
                        cmds.append(m.group(1))
                        idx += 2
                        continue
                m_explicit = re.match(
                    r"^(?:\[tool(?:_call|_use|_exec)?\]|TOOL_EXEC:|EXEC:|Running command:)\s*(.+)$",
                    line,
                )
                if m_explicit:
                    saw_structured = True
                    cmds.append(m_explicit.group(1))
                idx += 1
            return ("supported" if saw_structured else "none"), cmds
        return "none", []

    def terminate_worker_preserving_job(w_pid: int, w_pgid: int, j_pid: int) -> None:
        try:
            os.killpg(w_pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError:
            try:
                os.kill(w_pid, signal.SIGTERM)
            except OSError:
                return
        kill_grace = float(os.environ.get("TH_KILL_GRACE_SEC", "10"))
        deadline = time.monotonic() + max(0.1, kill_grace)
        while time.monotonic() < deadline:
            if not pid_alive(w_pid):
                break
            time.sleep(0.1)
        if pid_alive(w_pid):
            try:
                if get_pgid(j_pid) != w_pgid:
                    os.killpg(w_pgid, signal.SIGKILL)
                else:
                    os.kill(w_pid, signal.SIGKILL)
            except OSError:
                pass
            for _ in range(10):
                if not pid_alive(w_pid):
                    break
                time.sleep(0.05)

    event_out = "none"
    detail_out = ""

    if (
        has_distinct_job
        and job_alive
        and worker_alive
        and worker_pid is not None
        and job_pid is not None
    ):
        req_file = task / "context_exit_requested.json"
        now_epoch = int(time.time())
        if req_file.is_file():
            try:
                req_data = json.loads(req_file.read_text(encoding="utf-8"))
                req_epoch = int(req_data.get("requested_at_epoch", now_epoch))
            except Exception:
                req_epoch = now_epoch
            exit_grace = int(os.environ.get("TH_EXIT_GRACE_SEC", "60"))
            if now_epoch - req_epoch >= exit_grace:
                w_pgid = get_pgid(worker_pid)
                j_pgid = get_pgid(job_pid)
                if (
                    w_pgid is None
                    or j_pgid is None
                    or w_pgid <= 0
                    or j_pgid <= 0
                    or w_pgid == j_pgid
                    or worker_pid == job_pid
                ):
                    event_out = "kill_deferred"
                    detail_out = f"긴 실행 대기 — job 그룹 분리 미검증(worker_pgid={w_pgid}, job_pgid={j_pgid})으로 강제 종료 보류"
                else:
                    meta = load_ctx_meta() or {}
                    tkey = str(meta.get("task_key") or task.name)
                    minimal_target = (
                        task / "WORKLOG.minimal.md"
                        if (task / "WORKLOG.md").exists()
                        else task / "WORKLOG.md"
                    )
                    try:
                        written_wl = write_minimal_worklog(
                            path=minimal_target,
                            task_key=tkey,
                            goal=task.name,
                            current_state=f"long_running_job_alive(pid={job_pid})",
                            checkpoint=str(task),
                            tests="unknown",
                            error="worker_exit_timeout_after_long_running_wait",
                            next_step="wait_for_job_completion_then_fresh_analysis",
                            reason="long_running_wait",
                            experiment_path=meta.get("experiment_path"),
                        )
                        if (
                            meta.get("registry_path")
                            and Path(meta["registry_path"]).is_file()
                        ):
                            reg = WorkerContextRegistry(meta["registry_path"])
                            for item in reg.sessions():
                                if (
                                    item.get("session_id") == meta.get("session_id")
                                    and item.get("state") == "active"
                                ):
                                    reg.register_active(
                                        {
                                            **item,
                                            "worklog_path": str(written_wl),
                                            "long_running_wait_requested": True,
                                            "job_pid": job_pid,
                                        }
                                    )
                                    break
                    except Exception:
                        pass
                    terminate_worker_preserving_job(worker_pid, w_pgid, job_pid)
                    park_worker_if_needed(reason_override="long_running_wait")
                    event_out = "worker_terminated"
                    detail_out = f"긴 실행 대기 — 응답 제한시간({exit_grace}s) 초과로 최소 문서 작성 후 worker(pid={worker_pid}) 종료, job.pid({job_pid}) 보존"
        else:
            support_state, cmds = extract_worker_tool_events(task)
            if support_state == "unsupported":
                (task / ".th_long_wait_status").write_text(
                    "unsupported\n", encoding="utf-8"
                )
            elif cmds:
                tracker_path = task / ".th_wait_tracker.json"
                prev_tracker: dict = {}
                if tracker_path.is_file():
                    try:
                        prev_tracker = json.loads(
                            tracker_path.read_text(encoding="utf-8")
                        )
                    except Exception:
                        prev_tracker = {}
                cur_sig = artifact_signature()
                prev_count = int(prev_tracker.get("processed_cmds", 0))
                sleep_streak = int(prev_tracker.get("sleep_streak", 0))
                streak_sig = str(prev_tracker.get("artifact_sig", cur_sig))

                if prev_count > len(cmds):
                    prev_count = 0
                    sleep_streak = 0
                    streak_sig = cur_sig

                if sleep_streak > 0 and cur_sig != streak_sig:
                    sleep_streak = 0
                    streak_sig = cur_sig

                for cmd in cmds[prev_count:]:
                    kind = classify_tool_command(cmd)
                    if kind == "WORK":
                        sleep_streak = 0
                        streak_sig = cur_sig
                    elif kind == "SLEEP_POLL":
                        if sleep_streak == 0:
                            streak_sig = cur_sig
                        sleep_streak += 1

                write_json_atomic(
                    path=tracker_path,
                    payload={
                        "processed_cmds": len(cmds),
                        "sleep_streak": sleep_streak,
                        "artifact_sig": streak_sig,
                    },
                )
                if sleep_streak >= 2 and cur_sig == streak_sig:
                    req_payload = {
                        "action": "rollover",
                        "reason": "long_running_wait",
                        "requested_at": utc_timestamp(),
                        "requested_at_epoch": now_epoch,
                        "worker_pid": worker_pid,
                        "job_pid": job_pid,
                    }
                    write_json_atomic(path=req_file, payload=req_payload)
                    meta = load_ctx_meta()
                    if (
                        meta
                        and meta.get("registry_path")
                        and Path(meta["registry_path"]).is_file()
                    ):
                        try:
                            reg = WorkerContextRegistry(meta["registry_path"])
                            for item in reg.sessions():
                                if (
                                    item.get("session_id") == meta.get("session_id")
                                    and item.get("state") == "active"
                                ):
                                    reg.register_active(
                                        {
                                            **item,
                                            "long_running_wait_requested": True,
                                            "job_pid": job_pid,
                                        }
                                    )
                                    break
                        except Exception:
                            pass
                    event_out = "long_running_wait"
                    detail_out = f"긴 실행 대기 — job.pid({job_pid}) 생존 중 sleep·폴링 대기 연속 감지, 종료 요청(context_exit_requested.json)"

    # A distinct job.pid is the task's explicit declaration of a detached job.
    # Report its completion even if it exited before the watcher first observed
    # it alive; otherwise a silent job exit degrades to an ordinary dead worker.
    if (
        event_out == "none"
        and has_distinct_job
        and not job_alive
        and not (task / "DONE").is_file()
        and not (task / "FAILED").is_file()
    ):
        if worker_alive:
            event_out = "job_completed"
            detail_out = (
                f"잡 완료 — 분리 실행 job.pid({job_pid}) 종료, worker 인계 대기 중"
            )
        else:
            (task / ".th_job_completed").write_text(
                utc_timestamp() + "\n", encoding="utf-8"
            )
            meta = load_ctx_meta()
            if (
                meta
                and meta.get("registry_path")
                and Path(meta["registry_path"]).is_file()
            ):
                try:
                    reg = WorkerContextRegistry(meta["registry_path"])
                    for item in list(reg.sessions()):
                        if (
                            item.get("task_key") == meta.get("task_key")
                            and item.get("state") == "parked"
                        ):
                            if item.get("reason") == "long_running_wait" or item.get(
                                "long_running_wait_requested"
                            ):
                                reg.retire(
                                    str(item["session_id"]), reason="long_running_wait"
                                )
                except Exception:
                    pass
            exit_info = ""
            for ename in ("job.exit", "job_exit_code"):
                epath = task / ename
                if epath.is_file():
                    try:
                        exit_info = (
                            f" (exit={epath.read_text(encoding='utf-8').strip()})"
                        )
                    except OSError:
                        pass
                    break
            event_out = "job_completed"
            detail_out = f"잡 완료 — 분리 실행 job.pid({job_pid}){exit_info} 종료, 후속 결과 분석은 fresh 워커로 실행 필요"

    print(f"{event_out}\t{1 if (has_distinct_job and job_alive) else 0}\t{detail_out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
