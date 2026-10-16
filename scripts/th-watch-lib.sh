#!/usr/bin/env bash
# th-watch-lib.sh: th-watch 및 th-watchd 공통 감시·검수 함수 라이브러리
# 중복 구현 금지 원칙에 따라 사건 판정 및 검수 로직을 단일 정본으로 유지한다.

pid_alive() { [[ $1 =~ ^[1-9][0-9]*$ ]] && kill -0 "$1" 2>/dev/null; }

cpu_ticks() {
  local pid=$1 proc total=0 ticks child index=0
  [[ $pid =~ ^[1-9][0-9]*$ ]] || { echo 0; return; }
  local procs=("$pid")
  local seen=" $pid "
  local sid
  sid=$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ') || true
  if [[ -n $sid && $sid == "$pid" ]]; then
    while IFS= read -r sproc; do
      sproc=${sproc//[[:space:]]/}
      [[ $sproc =~ ^[1-9][0-9]*$ && $seen != *" $sproc "* ]] || continue
      seen="$seen$sproc "
      procs+=("$sproc")
    done < <(ps -o pid= -s "$sid" 2>/dev/null)
  fi

  while (( index < ${#procs[@]} )); do
    proc=${procs[index]}
    while IFS= read -r child; do
      child=${child//[[:space:]]/}
      [[ $child =~ ^[1-9][0-9]*$ && $seen != *" $child "* ]] || continue
      seen="$seen$child "
      procs+=("$child")
    done < <(ps -o pid= --ppid "$proc" 2>/dev/null)
    ((index += 1))
  done

  for proc in "${procs[@]}"; do
    ticks=$(awk -F') ' '{split($2, a, " "); print a[12] + a[13]}' "/proc/$proc/stat" 2>/dev/null) || continue
    [[ $ticks =~ ^[0-9]+$ ]] || continue
    total=$(( total + ticks ))
  done
  echo "$total"
}

remove_task_from_registry() {
  local task=$1 watch_dir=$2 registry="$2/tasks.txt" tmp
  (
    flock -x 200
    tmp=$(mktemp "$watch_dir/tasks.XXXXXX") || exit 1
    awk -v task="$task" '$0 != task' "$registry" > "$tmp" && mv "$tmp" "$registry"
  ) 200>"$watch_dir/tasks.lock"
}

latest_signature() {
  # Include actual artifacts, excluding digest/watch bookkeeping and input files.
  find "$1" -maxdepth 3 -type f ! -name '.th_*' \
    ! -name 'digest.md' ! -name 'verify_result.*' ! -name VERIFIED ! -name VERIFY_FAILED \
    ! -name task.md ! -name verify.sh ! -name '*.pid' ! -name 'workers*.md' \
    ! -name 'context_exit_requested.json' \
    -printf '%T@ %p\n' 2>/dev/null | sort
}

worker_digest_program() {
  local lib_dir
  lib_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  printf '%s/../src/team_harness/worker_digest.py\n' "$lib_dir"
}

run_verification() {
  local task=$1 program
  program=$(worker_digest_program)
  python3 "$program" verify "$task" >/dev/null 2>&1 || true
  if [[ -f $task/VERIFY_FAILED ]]; then
    echo "실패"
  elif [[ ! -f $task/verify.sh ]]; then
    echo "없음"
  else
    echo "통과"
  fi
}

quota_error_line() {
  local log=$1 suffix=${TH_WORKER_SUFFIX:-$HOME/.team-harness/worker_suffix.md}
  # Logs begin by echoing the worker prompt and may contain diffs/source code.
  # Match only forms emitted by CLIs, and discard prompt/markdown/code lines.
  awk -v suffix="$suffix" '
    BEGIN {
      if ((getline line < suffix) > 0) {
        do { prompt[line] = 1 } while ((getline line < suffix) > 0)
        close(suffix)
      }
    }
    {
      if (prompt[$0]) next
      if ($0 ~ /^[[:space:]]*([-*+#]|>|[0-9]+[.)])[[:space:]]/) next
      if ($0 ~ /^[+-][^+-]/ || $0 ~ /^@@ /) next
      if ($0 ~ /^ERROR:[[:space:]]*test_/) next
      lower = tolower($0)
      if (lower ~ /→[[:space:]]*`?failed`?/ || lower ~ /error[[:space:]]*→[[:space:]]*failed/) next
      if (lower ~ /(if[[:space:]]*\(|re\.search|awk[[:space:]]|grep[[:space:]]|~[[:space:]]*\/|last[[:space:]]*=[[:space:]]*\$0)/) next
      if (lower ~ /^([0-9]{4}-[0-9]{2}-[0-9]{2}[t ][0-9:.+-]+[[:space:]]+)?(error[: ]|api error[: ]|api response[: ]|agy_error[: ]|stream error[: ]|fatal[: ]|resource_exhausted[: ]|\{"(type":"error|error":))/ &&
          lower ~ /(hit your usage limit|usage limit reached|individual quota reached|rate limit (exceeded|reached)|resource_exhausted|quota exceeded|at capacity|capacity exceeded|([[:space:]:=]|status|http|error)429([^0-9]|$)|429([[:space:]]|$))/) {
        last = $0
      }
    }
    END { if (last != "") print substr(last, 1, 180) }
  ' "$log" 2>/dev/null
}

inspect_context_and_job() {
  local task=$1 lib_dir py_src
  lib_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
  py_src=$(cd "$lib_dir/../src" && pwd)
  # team_harness needs the project dependency graph (httpx); not system python3.
  PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" uv run --directory "$lib_dir/.." python - "$task" << 'PYEOF'
import hashlib, json, os, re, signal, sys, time
from pathlib import Path
from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.session_capture import _read_prefix_and_tail, extract_session_id
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.tracking.persistence import write_json_atomic
from team_harness.worker_context import (
    WorkerContextError,
    WorkerContextRegistry,
    measure_agent_context,
    parse_task_front_matter,
    registry_path_for_workspace,
    utc_timestamp,
    write_minimal_worklog,
)

task = Path(sys.argv[1]).resolve()

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

def get_pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None

worker_pid = read_pid(task / "worker.pid")
job_pid = read_pid(task / "job.pid")
worker_alive = pid_alive(worker_pid) if worker_pid is not None else False
# A job.pid identical to worker.pid is the worker itself, never a detached long job.
has_distinct_job = job_pid is not None and job_pid != worker_pid
job_alive = pid_alive(job_pid) if has_distinct_job and job_pid is not None else False

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
                    if item.get("task_key") == tkey and item.get("state") == "active":
                        return {
                            "registry_path": str(reg_path),
                            "workspace": str(ws),
                            "task_key": tkey,
                            "follow_up": fup,
                            "session_id": str(item.get("session_id")),
                            "agent_type": str(item.get("agent_type", "codex")),
                            "worklog_path": item.get("worklog_path") or str(task / "WORKLOG.md"),
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
    sessions_root = Path(os.environ.get("HOME", str(Path.home()))) / ".codex" / "sessions"
    measurement = measure_agent_context(
        agent_type=agent_type, session_id=resolved_sid, sessions_root=sessions_root
    )
    reason = reason_override
    if reason is None:
        if (task / "context_exit_requested.json").is_file() or (has_distinct_job and job_alive):
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
            if fname in {"STATUS", "WORKLOG.md", "WORKLOG.minimal.md", "EXPERIMENT.md"}:
                try:
                    digest = hashlib.sha1(fpath.read_bytes()).hexdigest()[:12]
                except OSError:
                    digest = str(st.st_mtime_ns)
                entries.append(f"{rel}:{digest}")
            else:
                entries.append(f"{rel}:{st.st_mtime_ns}:{st.st_size}")
    return "|".join(entries)

_SLEEP_CMD_RE = re.compile(r"(?:^|[;&|()\s])sleep\s+[0-9]+(?:\.[0-9]+)?(?:\s|$|[;&|)])")
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
    m = re.match(r"^(?:/usr/bin/env\s+)?(?:/bin/)?(?:ba)?sh\s+-[l]?c\s+(['\"])(.*)\1$", stripped, re.DOTALL)
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
        if exe == "sleep" and len(tokens) >= 2 and re.match(r"^[0-9]+(?:\.[0-9]+)?[smhd]?$", tokens[1].rstrip(")")):
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
        if item_type in {"command_execution", "exec_command", "tool_call", "tool_use", "function_call"}:
            if item_id:
                if item_id in seen_ids:
                    return []
                seen_ids.add(item_id)
            cmd = item.get("command") or item.get("cmd")
            if isinstance(cmd, str) and cmd.strip():
                cmds.append(cmd)
    if ev_type in {"command_execution", "exec_command", "tool_call", "tool_use", "function_call"}:
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
    if (tdir / "agy.log").is_file() and not (tdir / "codex.log").is_file() and not (tdir / "claude.log").is_file():
        return "unsupported", []
    for name in ("codex.log", "claude.log", "worker.log"):
        log_path = tdir / name
        if not log_path.is_file():
            continue
        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
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
                m = re.match(r"^(/bin/(?:ba)?sh\s+-[l]?c\s+.+?)(?:\s+in\s+\S+)?$", nxt)
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

if has_distinct_job and job_alive and worker_alive and worker_pid is not None and job_pid is not None:
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
            if w_pgid is None or j_pgid is None or w_pgid <= 0 or j_pgid <= 0 or w_pgid == j_pgid or worker_pid == job_pid:
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
                    if meta.get("registry_path") and Path(meta["registry_path"]).is_file():
                        reg = WorkerContextRegistry(meta["registry_path"])
                        for item in reg.sessions():
                            if item.get("session_id") == meta.get("session_id") and item.get("state") == "active":
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
            (task / ".th_long_wait_status").write_text("unsupported\n", encoding="utf-8")
        elif cmds:
            tracker_path = task / ".th_wait_tracker.json"
            prev_tracker: dict = {}
            if tracker_path.is_file():
                try:
                    prev_tracker = json.loads(tracker_path.read_text(encoding="utf-8"))
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
                if meta and meta.get("registry_path") and Path(meta["registry_path"]).is_file():
                    try:
                        reg = WorkerContextRegistry(meta["registry_path"])
                        for item in reg.sessions():
                            if item.get("session_id") == meta.get("session_id") and item.get("state") == "active":
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

# Check detached long job completion when neither worker nor job is alive.
if event_out == "none" and not worker_alive and not job_alive:
    was_detached_job = (
        seen_job_file.is_file()
        or (
            has_distinct_job
            and (
                (task / "context_exit_requested.json").is_file()
                or (task / "WORKLOG.md").is_file()
                or (task / "WORKLOG.minimal.md").is_file()
                or (task / "job.done").is_file()
                or (task / "job.exit").is_file()
            )
        )
    )
    if was_detached_job and not (task / "DONE").is_file() and not (task / "FAILED").is_file():
        (task / ".th_job_completed").write_text(utc_timestamp() + "\n", encoding="utf-8")
        meta = load_ctx_meta()
        if meta and meta.get("registry_path") and Path(meta["registry_path"]).is_file():
            try:
                reg = WorkerContextRegistry(meta["registry_path"])
                for item in list(reg.sessions()):
                    if item.get("task_key") == meta.get("task_key") and item.get("state") == "parked":
                        if item.get("reason") == "long_running_wait" or item.get("long_running_wait_requested"):
                            reg.retire(str(item["session_id"]), reason="long_running_wait")
            except Exception:
                pass
        exit_info = ""
        for ename in ("job.exit", "job_exit_code"):
            epath = task / ename
            if epath.is_file():
                try:
                    exit_info = f" (exit={epath.read_text(encoding='utf-8').strip()})"
                except OSError:
                    pass
                break
        event_out = "job_completed"
        detail_out = f"잡 완료 — 분리 실행 job.pid({job_pid}){exit_info} 종료, 후속 결과 분석은 fresh 워커로 실행 필요"

print(f"{event_out}\t{1 if (has_distinct_job and job_alive) else 0}\t{detail_out}")
PYEOF
}

inspect_task_event() {
  local task=$1 watch_dir=$2 marker_prefix=$3 stall_sec=$4 target_workspace=$5 callback_fn=$6
  [[ -n $task && -d $task ]] || return 0
  local marker="$task/$marker_prefix"

  local owner
  owner=$(cat "$task/.th_owner" 2>/dev/null || true)
  if [[ -z $owner && -n $target_workspace ]]; then
    local journal_log="$target_workspace/.claude/work_log.md"
    local journal_logs=("$(dirname "$journal_log")"/work_log*.md)
    if (( ${#journal_logs[@]} > 0 )) && grep -qsF "| $task |" "${journal_logs[@]}"; then
      owner=$target_workspace
    else
      owner="(unknown)"
    fi
  fi

  # target_workspace가 지정되어 있고 본인 소유가 아닌 경우:
  # 쿼터 잠금만 생성하고 사건 통지는 건너뜀
  if [[ -n $target_workspace && -n $owner && $owner != "$target_workspace" ]]; then
    for cand in "$task/codex.log" "$task/agy.log" "$task/claude.log"; do
      [[ -f $cand ]] || continue
      local lock="$watch_dir/quota_lock.$(basename "$cand" .log)"
      [[ -f $lock || -f $task/DONE || -f $task/FAILED ]] && continue
      local qline
      qline=$(quota_error_line "$cand")
      [[ -n $qline ]] && printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$qline" > "$lock"
    done
    return 0
  fi

  local ctx_line ctx_event="" distinct_job_alive=0 ctx_detail=""
  ctx_line=$(inspect_context_and_job "$task" 2>/dev/null) || {
    echo "th-watch: context inspection failed for $task (team_harness import?)" >&2
    ctx_line=""
  }
  if [[ -n $ctx_line ]]; then
    IFS=$'\t' read -r ctx_event distinct_job_alive ctx_detail <<< "$ctx_line"
  fi

  local terminal failed_file program
  program=$(worker_digest_program)
  terminal=$(python3 "$program" terminal "$task")
  failed_file="$task/FAILED"
  if [[ ! -f $failed_file ]]; then
    for cand in "$task"/FAILED.*; do
      [[ -f $cand ]] && { failed_file=$cand; break; }
    done
  fi

  # Failures take precedence over a stale DONE marker.
  # 1. 완료 판정 (단, 분리된 긴 job.pid가 아직 실행 중이면 인계만으로 최종 DONE 처리하지 않음)
  if [[ $terminal == DONE && $distinct_job_alive != 1 ]]; then
    if [[ ! -f $marker.done ]]; then
      touch "$marker.done"
      local v_res
      if [[ $marker_prefix == .th_daemon ]]; then
        [[ -f $task/verify.sh ]] && v_res="대기" || v_res="없음"
      else
        v_res=$(run_verification "$task")
      fi
      "$callback_fn" "$task" "완료" "완료" "$v_res" "$owner"
    fi
    return 0
  fi

  # 2. 실패 판정
  if [[ $terminal == FAILED ]]; then
    if [[ ! -f $marker.failed ]]; then
      touch "$marker.failed"
      local reason
      reason=$(tail -n 5 "$failed_file" 2>/dev/null | tr '\n' ' ')
      "$callback_fn" "$task" "실패" "실패 — $reason" "-" "$owner"
    fi
    return 0
  fi

  # 2b. 긴 실행 대기 감지 / 강제 종료 / 분리 job 완료 사건
  if [[ $ctx_event == "long_running_wait" && ! -f $marker.long_wait ]]; then
    touch "$marker.long_wait"
    "$callback_fn" "$task" "긴 실행 대기" "$ctx_detail" "-" "$owner"
  elif [[ $ctx_event == "kill_deferred" && ! -f $marker.kill_deferred ]]; then
    touch "$marker.kill_deferred"
    "$callback_fn" "$task" "긴 실행 대기" "$ctx_detail" "-" "$owner"
  elif [[ $ctx_event == "worker_terminated" && ! -f $marker.worker_terminated ]]; then
    touch "$marker.worker_terminated"
    "$callback_fn" "$task" "긴 실행 대기" "$ctx_detail" "-" "$owner"
  elif [[ $ctx_event == "job_completed" ]]; then
    if [[ ! -f $marker.job_done ]]; then
      touch "$marker.job_done"
      "$callback_fn" "$task" "잡 완료" "$ctx_detail" "-" "$owner"
    fi
    return 0
  fi

  # 3. 워커 오류(쿼터·용량)
  local log_file=""
  for cand in "$task/codex.log" "$task/agy.log" "$task/claude.log" "$task/worker.log"; do
    [[ -f $cand ]] || continue
    if [[ -n $(quota_error_line "$cand") ]]; then
      log_file=$cand
      break
    fi
  done
  if [[ -n $log_file && ! -f $marker.quota ]]; then
    touch "$marker.quota" "$marker.dead"
    local line
    line=$(quota_error_line "$log_file")
    local lock_type
    lock_type=$(basename "$log_file" .log)
    [[ $lock_type == worker ]] && lock_type=unknown
    printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$line" > "$watch_dir/quota_lock.$lock_type"
    "$callback_fn" "$task" "워커 오류(쿼터·용량)" "워커 오류(쿼터·용량) — $line (새 $lock_type 실행 잠금: $watch_dir/quota_lock.$lock_type)" "-" "$owner"
    # A quota event is non-terminal.  Continue so a later DONE/FAILED marker is
    # still observed on the next pass (and dead-process handling remains intact).
  fi

  # 4. 프로세스 상태 확인 (job.pid / worker.pid)
  local alive_pids=()

  while IFS= read -r p; do
    [[ $p =~ ^[1-9][0-9]*$ ]] && alive_pids+=("$p")
  done < <(python3 "$program" pids "$task")

  if (( ${#alive_pids[@]} == 0 )); then
    if [[ -f $task/DONE || -f $task/FAILED || -f $task/.th_job_completed ]]; then
      return 0
    fi
    if [[ ! -f $marker.dead ]]; then
      touch "$marker.dead"
      "$callback_fn" "$task" "중단" "중단 — 실행 중인 프로세스 없음, DONE/FAILED 없음" "-" "$owner"
    fi
    return 0
  fi

  rm -f "$marker.dead"

  # 5. 멈춤(stall) 판정
  local cpu=0
  for p in "${alive_pids[@]}"; do cpu=$(( cpu + $(cpu_ticks "$p") )); done
  local newest
  newest=$(latest_signature "$task")
  local signature="$cpu:$newest"
  local state_file="$task/.th_sig"
  local previous=""
  [[ -f $state_file ]] && previous=$(<"$state_file")
  local now
  now=$(date +%s)

  if [[ ${previous%%|*} != "$signature" ]]; then
    rm -f "$marker.stall"
    printf '%s|%s\n' "$signature" "$now" > "$state_file"
  else
    local since=${previous##*|}
    if [[ $since =~ ^[0-9]+$ ]] && (( now - since >= stall_sec )) && [[ ! -f $marker.stall ]]; then
      touch "$marker.stall"
      "$callback_fn" "$task" "멈춤" "멈춤 — CPU·산출물 $(( now - since ))s 변화 없음(프로세스는 살아 있음)" "-" "$owner"
    fi
  fi
}
