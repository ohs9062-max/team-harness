#!/usr/bin/env bash
# th-launch: 최신 worker_suffix.md를 반드시 덧붙여 워커(codex|agy|claude)를 분리 실행하고 등록한다.
# 사용: th-launch.sh <작업폴더> [codex|agy] <모델> <effort> <작업트리> [추가 인자]
#       Claude 워커는 사용자가 명시했을 때만 TH_USER_REQUESTED_CLAUDE=1을 붙여 실행:
#       TH_USER_REQUESTED_CLAUDE=1 th-launch.sh <작업폴더> claude claude-sonnet-5 <effort> <작업트리>
#       (워커 생략 시 기본값: codex)
#       Codex 모델 생략: <작업폴더> [codex] [작업트리] [추가 인자] → gpt-6.1-terra medium
#       강제: codex 동시 2개, high/sol은 TH_HIGH_REASON, codex는 front matter kind: code만(아니면 TH_CODEX_REASON),
#       최근 5시간 codex 사용 90% 이상이면 거부. 우회 사유는 work_log에 남는다.
# 정책 변경은 다음 launch부터 적용하며 실행 중 워커는 교체하지 않는다.
set -euo pipefail

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/th-watch-lib.sh
source "$script_dir/th-watch-lib.sh"

rotate_journal_if_needed() {
  [[ -f $journal_log ]] || return 0
  local modified today archive
  modified=$(date -r "$journal_log" +%Y%m%d)
  today=$(date +%Y%m%d)
  [[ $modified == "$today" ]] && return
  archive="$(dirname "$journal_log")/work_log_${modified}.md"
  [[ -e $archive ]] && archive="${archive}.$(date +%H%M%S)"
  mv "$journal_log" "$archive"
}
journal_append() {
  mkdir -p "$(dirname "$journal_log")"
  rotate_journal_if_needed
  touch "$journal_log"
  local override_suffix=""
  if [[ -n ${policy_override:-} ]]; then
    override_suffix=" | $policy_override"
  fi
  printf '%s | 지시 | %s | worker=%s model=%s task.md=%s | %s%s\n' \
    "$(date '+%F %R')" "$task_dir" "$worker_type" "$model" "$task_dir/task.md" "$journal_workspace" "$override_suffix" >> "$journal_log"
}

(( $# >= 1 )) || { echo '사용: th-launch.sh <작업폴더> [codex|agy|claude] <모델> <effort> <작업트리> [추가 인자]'; exit 2; }
task_dir=$(realpath "$1"); shift

worker_type="codex"
if (( $# >= 1 )) && [[ "$1" == "codex" || "$1" == "agy" || "$1" == "claude" ]]; then
  worker_type="$1"
  shift
fi

if [[ "$worker_type" == "claude" && ${TH_USER_REQUESTED_CLAUDE:-0} != 1 ]]; then
  echo 'Claude 워커는 사용자 명령 시에만: TH_USER_REQUESTED_CLAUDE=1'
  exit 4
fi

if [[ "$worker_type" == "codex" ]]; then
  if (( $# >= 1 )) && [[ -d "$1" ]]; then
    model="gpt-6.1-terra"; effort="medium"; worktree=$1; shift
  elif (( $# == 0 )); then
    model="gpt-6.1-terra"; effort="medium"; worktree="$task_dir"
  else
    (( $# >= 3 )) || { echo '사용: th-launch.sh <작업폴더> [codex] [<모델> <effort>] <작업트리> [codex 추가 인자]'; exit 2; }
    model=$1; effort=$2; worktree=$3; shift 3
  fi
elif [[ "$worker_type" == "agy" ]]; then
  if (( $# >= 3 )); then
    model=$1; effort=$2; worktree=$3; shift 3
  elif (( $# == 2 )); then
    model=$1; effort="none"; worktree=$2; shift 2
  elif (( $# == 1 )); then
    model="Gemini 3.8 Flash (High)"; effort="none"; worktree=$1; shift 1
  else
    model="Gemini 3.8 Flash (High)"; effort="none"; worktree="$task_dir"
  fi
  if [[ -z "$model" || "$model" == "-" || "$model" == "default" ]]; then
    model="Gemini 3.8 Flash (High)"
  fi
elif [[ "$worker_type" == "claude" ]]; then
  if (( $# >= 3 )); then
    model=$1; effort=$2; worktree=$3; shift 3
  elif (( $# == 2 )); then
    model=$1; effort="none"; worktree=$2; shift 2
  elif (( $# == 1 )); then
    model=$1; effort="none"; worktree="$task_dir"; shift 1
  else
    model="claude-sonnet-5"; effort="none"; worktree="$task_dir"
  fi
  if [[ -z "$model" || "$model" == "-" || "$model" == "default" ]]; then
    model="claude-sonnet-5"
  fi
fi

pwd_workspace=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || true)
journal_workspace=${TH_WORKSPACE:-}
if [[ -z $journal_workspace ]]; then
  journal_workspace=$(resolve_launch_owner "$task_dir" "$worktree" "$PWD")
fi
journal_workspace=$(realpath "$journal_workspace")
journal_log="$journal_workspace/.claude/work_log.md"
if [[ -n $pwd_workspace ]] && pwd_workspace=$(realpath "$pwd_workspace"); then
  warn_launch_owner_mismatch "$journal_workspace" "$pwd_workspace"
fi

watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
# 동기화된 전역 사본만 사용한다. 임의의 오래된 경로를 override하지 않는다.
suffix=${TH_WORKER_SUFFIX:-"$HOME/.team-harness/worker_suffix.md"}
# 쿼터·용량 오류 뒤에는 같은 종류 워커를 새로 띄우지 않는다(th-watch가 잠금 생성).
quota_lock="$watch_dir/quota_lock.$worker_type"
if [[ -f $quota_lock && ${TH_IGNORE_QUOTA_LOCK:-0} != 1 ]]; then
  lock_text=$(<"$quota_lock")
  lock_expiry=""
  [[ $lock_text =~ (^|[[:space:]])expires=([0-9]+) ]] && lock_expiry=${BASH_REMATCH[2]}
  if [[ -z $lock_expiry || $lock_expiry -gt $(date +%s) ]]; then
    echo "쿼터 잠금으로 $worker_type 실행 거부: $lock_text"
    echo "쿼터가 풀렸으면 rm '$quota_lock' 후 다시 실행"
    exit 3
  fi
fi
[[ -f $task_dir/task.md ]] || { echo "task.md 없음: $task_dir"; exit 1; }
py_src=$(cd "$script_dir/../src" && pwd)
# worker_context imports the project dependency graph (including httpx).  Do
# not use the system interpreter here: th-launch may be started outside an
# activated development shell.
context_python=(th_python)
policy_rc=0
policy_out=$(PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" "${context_python[@]}" -m team_harness.launch_policy check "$task_dir" "$worker_type" "$model" "$effort" "$watch_dir") || policy_rc=$?
if (( policy_rc != 0 )); then
  exit "$policy_rc"
fi
if [[ $worker_type == codex && ${TEAM_HARNESS_JEV:-} != 0 ]]; then
  th_jev -m team_harness.decisions.advisor tier --task "$task_dir/task.md" --worker "$worker_type" --model "$model" --effort "$effort"
fi
policy_override=""
while IFS= read -r line; do
  if [[ $line == override:* ]]; then
    if [[ -n $policy_override ]]; then
      policy_override="$policy_override | $line"
    else
      policy_override="$line"
    fi
  fi
done <<< "$policy_out"
[[ -f $suffix ]] || { echo "worker_suffix.md 없음(최신 정책을 붙일 수 없음): $suffix"; exit 1; }

ctx_prep_file=$(mktemp "${TMPDIR:-/tmp}/th-launch-ctx.XXXXXX")
cleanup_ctx_prep() { rm -f "$ctx_prep_file"; }
trap cleanup_ctx_prep EXIT

if ! PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" "${context_python[@]}" - "$journal_workspace" "$task_dir" "$worker_type" "$model" "$worktree" "$ctx_prep_file" << 'PYEOF'
import json, os, re, shutil, sys, uuid
from pathlib import Path
from team_harness.agents.process_identity import capture_starttime
from team_harness.agents.session_capture import _read_prefix_and_tail, extract_session_id
from team_harness.agents.template import DEFAULT_AGENT_TEMPLATES
from team_harness.config import load_worker_context_settings
from team_harness.worker_context import (
    ContextRequest,
    RegistryConflictError,
    WorkerContextError,
    WorkerContextRegistry,
    dispatch,
    measure_agent_context,
    parse_task_allow_paths,
    parse_task_metadata,
    parse_task_front_matter,
    registry_path_for_workspace,
    should_prepare_handoff,
    write_minimal_worklog,
)

workspace = Path(sys.argv[1]).resolve()
task_dir = Path(sys.argv[2]).resolve()
worker_type = sys.argv[3]
model = sys.argv[4]
worktree = Path(sys.argv[5]).resolve() if Path(sys.argv[5]).exists() else Path(sys.argv[5])
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
    job_pid is not None
    and job_pid != worker_pid_prev
    and pid_alive(job_pid)
)
if not context_active:
    preserve_job = bool(
        distinct_job_alive
    )
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
sessions_root = Path(os.environ.get("HOME", str(Path.home()))) / ".codex" / "sessions"

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
        if recorded_start is None or current_start is None or current_start == recorded_start:
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
                sid, measurement, resolved_session_id=resolved_sid, reason="reaped_before_dispatch"
            )
        except WorkerContextError:
            pass

# Long-running wait completion forces fresh analysis even when follow_up is short.
force_fresh = (task_dir / ".th_job_completed").is_file()
for item in list(registry.sessions()):
    if item.get("task_key") != task_key or item.get("state") != "parked":
        continue
    if item.get("reason") == "long_running_wait" or item.get("long_running_wait_requested"):
        force_fresh = True
        try:
            registry.retire(str(item["session_id"]), reason="long_running_wait")
        except WorkerContextError:
            pass

effective_follow_up = "normal" if force_fresh else follow_up
try:
    decision, registry = dispatch(
        workspace=workspace,
        request=ContextRequest(
            task_key=task_key, agent_type=worker_type, follow_up=effective_follow_up,
            context_budget=context_budget,
        ),
        settings=settings,
    )
except WorkerContextError as exc:
    print(f"worker context error: {exc}", file=sys.stderr)
    sys.exit(1)

is_handoff = should_prepare_handoff(
    task_key=task_key,
    sessions=registry.sessions(),
    terminal=(task_dir / "DONE").is_file() or (task_dir / "FAILED").is_file(),
    context_exit_requested=(task_dir / "context_exit_requested.json").is_file() or force_fresh,
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
    candidate_wl = Path(decision.worklog_path) if decision.worklog_path else (task_dir / "WORKLOG.md")
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
        handoff_parts.append(f"## Handoff WORKLOG ({worklog_path})\n{wl_text.strip()}")
        if exp_doc is not None:
            try:
                exp_lines = exp_doc.read_text(encoding="utf-8").splitlines()
                if exp_lines and len(exp_lines) <= 80:
                    handoff_parts.append(f"## Handoff EXPERIMENT ({exp_doc})\n" + "\n".join(exp_lines))
                elif exp_lines:
                    handoff_parts.append(f"## Handoff EXPERIMENT\nPrior experiment is at {exp_doc}; it exceeds the 80-line handoff limit.")
            except OSError:
                pass
else:
    exp_doc = find_experiment_path(task_dir, raw_task)
    if exp_doc is not None:
        experiment_path = str(exp_doc)

if os.environ.get("TH_FAIL_LAUNCH") == "1" or shutil.which(worker_type) is None:
    print(f"worker launch failed: {worker_type} unavailable", file=sys.stderr)
    sys.exit(1)

spawn_mode = "resume" if decision.action == "resume" and decision.session_id else "fresh"
session_id = decision.session_id if spawn_mode == "resume" and decision.session_id else str(uuid.uuid4())
prior_entry = next(
    (dict(item) for item in registry.sessions() if item.get("session_id") == session_id),
    None,
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
PYEOF
then
  exit 1
fi

readarray -t ctx_meta < <(python3 - "$ctx_prep_file" << 'PYEOF'
import json, sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
print("1" if data.get("enabled") else "0")
print(data.get("mode", "fresh"))
print(data.get("session_id", ""))
print("1" if data.get("preserve_job") else "0")
PYEOF
)
ctx_enabled=${ctx_meta[0]:-0}
spawn_mode=${ctx_meta[1]:-fresh}
resume_session_id=${ctx_meta[2]:-}
preserve_job_pid=${ctx_meta[3]:-0}
handoff_prompt=$(python3 - "$ctx_prep_file" << 'PYEOF'
import json, sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(data.get("handoff_text", ""), end="")
PYEOF
)
worker_allow_paths=$(python3 - "$ctx_prep_file" << 'PYEOF'
import json, sys
data = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(json.dumps(data.get("allow_paths", [])))
PYEOF
)

session_agent=unknown
if [[ -n ${CLAUDECODE:-} ]]; then
  session_agent=claude
else
  while IFS='=' read -r name _; do
    [[ $name == CODEX_* ]] && session_agent=codex
  done < <(env)
  parent=$PPID
  while [[ $parent =~ ^[0-9]+$ && $parent -gt 1 && -r /proc/$parent/stat ]]; do
    comm=$(cat "/proc/$parent/comm" 2>/dev/null || true)
    [[ $comm == codex || $comm == codex-* ]] && session_agent=codex
    parent=$(awk '{print $4}' "/proc/$parent/stat" 2>/dev/null || printf 0)
  done
fi
printf '%s\n' "$session_agent" > "$task_dir/.th_session_agent"
journal_append
if [[ $session_agent != codex ]] && ! inbox_waiter_registered "$journal_workspace"; then
  printf '[th-launch] 경고: %s 코디네이터 inbox 대기 없음 — 사건이 전달되지 않을 수 있음\n' "$journal_workspace" >&2
  printf '%s | 경고 | inbox-wait 없음 | %s\n' "$(date '+%F %R')" "$task_dir" >> "$journal_log"
fi
mkdir -p "$watch_dir"; touch "$watch_dir/tasks.txt"
usage_record() {
  local usage_file="$watch_dir/usage.jsonl" attempt at owner_json override_json
  at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
  attempt=$(awk -v task="$task_dir" 'BEGIN { n=0 } $0 ~ /"task"/ && index($0, "\"task\":\"" task "\"") { n++ } END { print n + 1 }' "$usage_file" 2>/dev/null || printf '1')
  owner_json=$(printf '%s' "$journal_workspace" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
  override_json=$(printf '%s' "${policy_override:-}" | python3 -c 'import json,sys; x=sys.stdin.read(); print(json.dumps(x if x else None))')
  if ! (
    flock -x 200
    printf '{"v":1,"at":"%s","task":%s,"attempt":%s,"worker":%s,"model":%s,"effort":%s,"owner":%s,"override":%s}\n' \
      "$at" "$(printf '%s' "$task_dir" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$attempt" \
      "$(printf '%s' "$worker_type" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$(printf '%s' "$model" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
      "$(printf '%s' "$effort" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" "$owner_json" "$override_json" >> "$usage_file"
  ) 200>>"$usage_file"; then
    printf 'warning: usage ledger append failed; launch continues\n' >&2
  fi
}
usage_record
rm -f "$task_dir"/DONE "$task_dir"/FAILED "$task_dir"/FAILED.* "$task_dir"/.th_* \
  "$task_dir"/VERIFIED "$task_dir"/VERIFY_FAILED "$task_dir"/verify_result.json \
  "$task_dir"/verify_result.txt "$task_dir"/digest.md "$task_dir"/context_exit_requested.json
if [[ $preserve_job_pid != 1 ]]; then
  rm -f "$task_dir/job.pid"
fi
# A relaunch is a new attempt.  Make it the latest STATUS entry so a prior
# attempt's STATUS-only FAILED/DONE result cannot be mistaken for this one.
printf '%s LAUNCH worker=%s model=%s effort=%s\n' \
  "$(date '+%F %T')" "$worker_type" "$model" "$effort" >> "$task_dir/STATUS"
# 이 작업을 띄운 작업 공간. th-watch는 소유자 작업만 알린다(다른 코디네이터 사건을 가로채지 않게).
printf '%s\n' "$journal_workspace" > "$task_dir/.th_owner"
session_key=$(th_session_key)
if [[ -n $session_key ]]; then
  printf '%s\n' "$session_key" > "$task_dir/.th_session"
else
  rm -f "$task_dir/.th_session"
fi
printf '%s\n' "$worktree" > "$task_dir/.th_worktree"
if [[ -n ${TH_WORKERS_FILE:-} ]]; then
  printf '%s\n' "$TH_WORKERS_FILE" > "$task_dir/.th_workers_file"
fi

# 보조 검수: task.md의 ## 검수 블록을 verify.sh로 저장 (없으면 경고만)
verify_script="$task_dir/verify.sh"
rm -f "$verify_script"
awk '
  /^##[[:space:]]+검수/ { in_verify=1; next }
  in_verify && /^```bash/ { in_code=1; next }
  in_code && /^```/ { exit }
  in_code { print }
' "$task_dir/task.md" > "$verify_script" || true

if [[ -s "$verify_script" ]]; then
  chmod +x "$verify_script"
else
  rm -f "$verify_script"
  printf 'warning: task.md에 ## 검수 블록 없음 (보조 검수 생략)\n' >&2
fi

task_body="$(<"$task_dir/task.md")"
if [[ -n $handoff_prompt ]]; then
  task_body="$task_body

---
$handoff_prompt"
fi

# STATUS is the durable step record.  Preserve all earlier attempts (including
# before the most recent LAUNCH) and give a fresh worker only its small,
# deterministic recovery note.
recovery_summary=$(PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" th_python -m team_harness.step_records summary "$task_dir")
if [[ -n $recovery_summary ]]; then
  printf '%s' "$recovery_summary" > "$task_dir/RECOVERY.md"
  task_body="$task_body

---
$recovery_summary"
fi

worker_header='너는 team-harness 워커다(TH_ROLE=worker). 코디네이터가 아니다. 이 지시서의 작업만 하고 결과는 결과 폴더에만 남긴다.'
prompt="$worker_header

$task_body

---
$(<"$suffix")
- 결과 폴더: $task_dir. 분리 실행한 본 작업의 PID를 $task_dir/job.pid에 기록할 것. 끝나면 DONE, 실패 시 FAILED+이유를 남길 것."
cd "$task_dir"

# TH-D33: an ambient shell permission must never leak into a worker.  The
# coordinator may set this exact value only after the current user turn has
# explicitly requested image reading; otherwise every worker starts blocked.
image_env=("TH_ALLOW_IMAGES=0")
if [[ ${TH_ALLOW_IMAGES:-0} == 1 ]]; then
  image_env=("TH_ALLOW_IMAGES=1")
fi
timezone_env=()
if [[ ${TZ+x} ]]; then
  timezone_env=("TZ=$TZ")
else
  worker_tz=""
  if [[ -r /etc/timezone ]]; then
    worker_tz=$(tr -d '[:space:]' < /etc/timezone)
  fi
  if [[ -z $worker_tz ]] && command -v timedatectl >/dev/null 2>&1; then
    worker_tz=$(timedatectl show --property=Timezone --value 2>/dev/null || true)
  fi
  [[ -n $worker_tz ]] && timezone_env=("TZ=$worker_tz")
fi
worker_env=("TH_ROLE=worker" "TH_WORKSPACE=$journal_workspace" "TH_WORKER_OUTPUT_DIR=$task_dir" "TH_WORKER_ALLOW_PATHS=$worker_allow_paths" "TH_HARNESS_SRC=$py_src" "${image_env[@]}" "${timezone_env[@]}")

if [[ "$worker_type" == "codex" ]]; then
  project_doc_args=("-c" "project_doc_max_bytes=0")
  if [[ ${TH_CODEX_PROJECT_DOC:-0} == 1 ]]; then
    project_doc_args=()
  fi
  if [[ "$spawn_mode" == "resume" && -n "$resume_session_id" ]]; then
    worker_cmd=(codex exec resume -m "$model" -c "model_reasoning_effort=$effort" -c "mcp_servers={}" "${project_doc_args[@]}" --dangerously-bypass-approvals-and-sandbox -C "$worktree" "$resume_session_id")
  else
    worker_cmd=(codex exec -m "$model" -c "model_reasoning_effort=$effort" -c "mcp_servers={}" "${project_doc_args[@]}" --dangerously-bypass-approvals-and-sandbox -C "$worktree")
  fi
  env "${worker_env[@]}" setsid nohup "${worker_cmd[@]}" "$@" -- "$prompt" < /dev/null > codex.log 2>&1 &
  printf '%s\n' "$!" > "$task_dir/worker.pid"
elif [[ "$worker_type" == "agy" ]]; then
  add_dir_args=("--add-dir" "$task_dir")
  if [[ -n "$worktree" && "$worktree" != "$task_dir" && -d "$worktree" ]]; then
    add_dir_args+=("--add-dir" "$worktree")
  fi
  env "${worker_env[@]}" setsid nohup agy --dangerously-skip-permissions --model "$model" "${add_dir_args[@]}" "$@" --print="$prompt" < /dev/null > agy.log 2>&1 &
  init_pid=$!

  # agy는 실행 직후 별도 PID로 재부모화될 수 있다. 최초 nohup PID는
  # 그 전환 중 끝나므로, 그것을 제외하고 결과 폴더를 인자로 가진 agy를
  # 최대 20초 동안 찾는다. 이 탐색은 launch 시 한 번만 수행한다.
  actual_pid=""
  deadline=$((SECONDS + 20))
  while (( SECONDS < deadline )) && [[ -z $actual_pid ]]; do
    for p in /proc/[0-9]*/cmdline; do
      pid=${p#/proc/}
      pid=${pid%/cmdline}
      [[ $pid != "$$" && $pid != "$init_pid" ]] || continue
      [[ -r $p ]] || continue
      cmd=$(tr '\0' ' ' < "$p" 2>/dev/null || true)
      exe=${cmd%% *}
      [[ ${exe##*/} == "agy" ]] || continue
      [[ $cmd == *"$task_dir"* ]] || continue
      kill -0 "$pid" 2>/dev/null || continue
      actual_pid="$pid"
      break
    done
    [[ -n $actual_pid ]] || sleep 0.2
  done

  if [[ -n "$actual_pid" ]]; then
    printf '%s\n' "$actual_pid" > "$task_dir/worker.pid"
  else
    printf 'warning: agy reparented PID not found within 20s; recording initial PID %s\n' "$init_pid" >&2
    printf '%s\n' "$init_pid" > "$task_dir/worker.pid"
  fi
elif [[ "$worker_type" == "claude" ]]; then
  if [[ "$spawn_mode" == "resume" && -n "$resume_session_id" ]]; then
    worker_cmd=(claude -p --dangerously-skip-permissions --model "$model" --strict-mcp-config --resume "$resume_session_id")
  else
    worker_cmd=(claude -p --dangerously-skip-permissions --model "$model" --strict-mcp-config)
  fi
  env "${worker_env[@]}" setsid nohup "${worker_cmd[@]}" "$@" -- "$prompt" < /dev/null > claude.log 2>&1 &
  printf '%s\n' "$!" > "$task_dir/worker.pid"
fi

if [[ $ctx_enabled == 1 ]]; then
  if ! PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" "${context_python[@]}" - "$ctx_prep_file" "$task_dir" << 'PYEOF'
import json, os, sys
from pathlib import Path
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
PYEOF
  then
    exit 1
  fi
fi

(
  flock -x 200
  grep -qxF "$task_dir" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\n' "$task_dir" >> "$watch_dir/tasks.txt"
) 200>"$watch_dir/tasks.lock"

# 상시 감시 데몬: 데몬이 없으면 setsid nohup으로 띄움
if [[ ${TH_NO_DAEMON:-0} != 1 && -x "$script_dir/th-watchd.sh" ]]; then
  touch "$watch_dir/watchd.lock"
  if flock -n "$watch_dir/watchd.lock" true 2>/dev/null; then
    setsid nohup "$script_dir/th-watchd.sh" > /dev/null 2>&1 &
  fi
fi

printf 'launched [%s] pid %s → %s\n' "$worker_type" "$(<"$task_dir/worker.pid")" "$task_dir"

inbox_owner="$journal_workspace"
if inbox_waiter_registered "$inbox_owner"; then
  printf '[th-launch] th-inbox-wait 실행 중\n'
fi
