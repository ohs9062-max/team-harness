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

script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
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
py_src=$(cd "$script_dir/../src" && pwd)
if [[ ${TH_PREFLIGHT_ONLY:-0} != 1 && ${TH_AUTO_RECALL:-1} != 0 ]]; then
  recall_key=$(sed -n 's/^task_key:[[:space:]]*//p' "$task_dir/task.md" 2>/dev/null | head -n 1 || true)
  timeout 2s "$script_dir/th-env.sh" -m team_harness.recall_hook launch "$task_dir" "$recall_key" 2>/dev/null || true
fi
# Preflight must remain usable while a worker is alive. Real launches serialize
# all checks and mutations for the task until the new identity is registered.
if [[ ${TH_PREFLIGHT_ONLY:-0} != 1 ]]; then
  exec {launch_lock_fd}>"$task_dir/.th_launch.lock"
  flock -x "$launch_lock_fd"
  if th_python "$script_dir/th-launch-identity.py" check "$task_dir"; then
    echo '이미 실행 중'
    exit 75
  fi
fi
if [[ ${TH_ROLLOVER:-0} == 1 || ${TH_AUTO_CONTINUE:-0} == 1 || ${TH_RELAUNCH:-0} == 1 ]]; then
  unset TH_RETRY_REASON
  eval "$(th_python -m team_harness.launch_env load "$task_dir")"
fi

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

if [[ ! -d "$worktree" ]]; then
  printf "error: worktree not found: %s\n" "$worktree" >&2
  exit 1
fi
if [[ "$worktree" != /* ]]; then
  worktree=$(realpath "$worktree")
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
  [[ $lock_text =~ (^|[[:space:]])expires=([0-9]+)($|[[:space:]]) ]] && lock_expiry=${BASH_REMATCH[2]}
  if [[ -z $lock_expiry || $lock_expiry -gt $(date +%s) ]]; then
    echo "쿼터 잠금으로 $worker_type 실행 거부: $lock_text"
    echo "쿼터가 풀렸으면 rm '$quota_lock' 후 다시 실행"
    exit 3
  fi
fi
[[ -f $task_dir/task.md ]] || { echo "task.md 없음: $task_dir"; exit 1; }
# worker_context imports the project dependency graph (including httpx).  Do
# not use the system interpreter here: th-launch may be started outside an
# activated development shell.
context_python=(th_python)
policy_rc=0
policy_out=$("${context_python[@]}" -m team_harness.launch_policy check "$task_dir" "$worker_type" "$model" "$effort" "$watch_dir") || policy_rc=$?
if (( policy_rc != 0 )); then
  printf '%s\n' "$policy_out" >&2
  exit "$policy_rc"
fi
if [[ $worker_type == codex && ${TEAM_HARNESS_JEV:-} != 0 && ${TH_PREFLIGHT_ONLY:-0} != 1 ]]; then
  th_jev -m team_harness.decisions.advisor tier --task "$task_dir/task.md" --worker "$worker_type" --model "$model" --effort "$effort"
fi
brief_rc=0
"${context_python[@]}" -m team_harness.brief_check "$task_dir" || brief_rc=$?
if (( brief_rc != 0 )); then
  if (( brief_rc != 2 )) || [[ -z ${TH_BRIEF_OK:-} || -z ${TH_BRIEF_OK//[[:space:]]/} ]]; then
    echo '지시서 점검 실패: 수정하거나 TH_BRIEF_OK에 우회 사유를 지정하세요' >&2
    exit "$brief_rc"
  fi
fi
if [[ ${TH_PREFLIGHT_ONLY:-0} == 1 ]]; then
  exit 0
fi
policy_override=""
if (( brief_rc == 2 )); then
  policy_override="override: TH_BRIEF_OK=$TH_BRIEF_OK"
fi
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
admission_slot=""
worker_spawned=0
cleanup_ctx_prep() {
  # Context registration is provisional until a process is started. Any
  # preparation failure must restore the former parked entry or remove it.
  if [[ $worker_spawned == 0 && -s $ctx_prep_file ]]; then
    th_python -m team_harness.launch_prep cleanup "$ctx_prep_file" || true
  fi
  rm -f "$ctx_prep_file"
  if [[ -n $admission_slot ]]; then
    th_python -m team_harness.resource_slots release "$admission_slot" || true
  fi
}
trap cleanup_ctx_prep EXIT

# Reserve before registry preparation, durable attempt recording, or clearing
# any previous result. A losing concurrent launcher leaves the task intact.
admission_slot=$(th_python -m team_harness.resource_slots reserve "$task_dir" "$worker_type" "$model" "$effort" "$$")
if [[ ${TH_ROLLOVER:-0} != 1 && ${TH_AUTO_CONTINUE:-0} != 1 && ${TH_RELAUNCH:-0} != 1 ]]; then
  th_python -m team_harness.launch_env save "$task_dir"
fi

if ! "${context_python[@]}" -m team_harness.launch_prep prepare "$journal_workspace" "$task_dir" "$worker_type" "$model" "$worktree" "$ctx_prep_file"
then
  exit 1
fi

readarray -t ctx_meta < <(th_python -m team_harness.launch_prep meta "$ctx_prep_file"
)
ctx_enabled=${ctx_meta[0]:-0}
spawn_mode=${ctx_meta[1]:-fresh}
resume_session_id=${ctx_meta[2]:-}
preserve_job_pid=${ctx_meta[3]:-0}
handoff_prompt=$(th_python -m team_harness.launch_prep handoff "$ctx_prep_file"
)
worker_allow_paths=$(th_python -m team_harness.launch_prep allow-paths "$ctx_prep_file"
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
  if ! (
    flock -x 200
    th_python -m team_harness.launch_prep usage "$at" "$task_dir" "$attempt" "$worker_type" "$model" "$effort" "$journal_workspace" "${policy_override:-}" "$( [[ ${TH_ROLLOVER:-0} == 1 ]] && printf true || printf false )" >> "$usage_file"
  ) 200>>"$usage_file"; then
    printf 'warning: usage ledger append failed; launch continues\n' >&2
  fi
}
usage_record
# These flags describe this launch only; child workers must not exempt their
# own later launches from normal retry accounting.
unset TH_AUTO_CONTINUE TH_ROLLOVER
# Attempt markers are reset, but policy overrides must survive every relaunch.
for marker in "$task_dir"/.th_*; do
  [[ -d $marker ]] && continue
  [[ $marker == "$task_dir/.th_launch_env" || $marker == "$task_dir/.th_launch.lock" ]] || rm -f "$marker"
done
rm -f "$task_dir"/DONE "$task_dir"/FAILED "$task_dir"/FAILED.* \
  "$task_dir"/VERIFIED "$task_dir"/VERIFY_NONE "$task_dir"/VERIFY_FAILED "$task_dir"/verify_result.json \
  "$task_dir"/verify_result.txt "$task_dir"/digest.md "$task_dir"/context_exit_requested.json
if [[ $preserve_job_pid != 1 ]]; then
  rm -f "$task_dir/job.pid"
fi
# A relaunch is a new attempt.  Make it the latest STATUS entry so a prior
# attempt's STATUS-only FAILED/DONE result cannot be mistaken for this one.
printf '%s LAUNCH worker=%s model=%s effort=%s\n' \
  "$(date '+%F %T')" "$worker_type" "$model" "$effort" >> "$task_dir/STATUS"
# 이 작업을 띄운 작업 공간. th-watch는 소유자 작업만 알린다(다른 코디네이터 사건을 가로채지 않게).
printf '%s\n' "$session_agent" > "$task_dir/.th_session_agent"
printf '%s\n' "$journal_workspace" > "$task_dir/.th_owner"
session_key=$(th_session_key)
th_python "$script_dir/th-coordinator.py" "$PPID" "$session_key" "$task_dir/.th_coordinator.json"
mkdir -p "$journal_workspace/.claude"
touch "$journal_workspace/.claude/inbox.md"
th_python "$script_dir/th-inbox-read.py" "$journal_workspace" 1 init || exit 2
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
recovery_summary=$(th_python -m team_harness.step_records summary "$task_dir")
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
adopt_timeout=()
if [[ ${TH_ADOPT_TIMEOUT_SEC:-} =~ ^[1-9][0-9]*$ ]]; then
  adopt_timeout=(timeout --signal=TERM --kill-after=5 "${TH_ADOPT_TIMEOUT_SEC}s")
fi

if [[ "$worker_type" == "codex" ]]; then
  project_doc_args=("-c" "project_doc_max_bytes=0")
  if [[ ${TH_CODEX_PROJECT_DOC:-0} == 1 ]]; then
    project_doc_args=()
  fi
  if [[ "$spawn_mode" == "resume" && -n "$resume_session_id" ]]; then
    worker_cmd=(codex exec resume -m "$model" -c "model_reasoning_effort=$effort" -c "mcp_servers={}" "${project_doc_args[@]}" --dangerously-bypass-approvals-and-sandbox "$resume_session_id")
  else
    worker_cmd=(codex exec -m "$model" -c "model_reasoning_effort=$effort" -c "mcp_servers={}" "${project_doc_args[@]}" --dangerously-bypass-approvals-and-sandbox -C "$worktree")
  fi
  (
    if [[ "$spawn_mode" == "resume" && -n "$resume_session_id" ]]; then cd "$worktree"; fi
    exec {launch_lock_fd}>&-
    exec env -u TH_JOB_TASK "${worker_env[@]}" setsid nohup "${adopt_timeout[@]}" "${worker_cmd[@]}" "$@" -- "$prompt"
  ) < /dev/null > codex.log 2>&1 &
  printf '%s\n' "$!" > "$task_dir/worker.pid"
elif [[ "$worker_type" == "agy" ]]; then
  add_dir_args=("--add-dir" "$task_dir")
  if [[ -n "$worktree" && "$worktree" != "$task_dir" && -d "$worktree" ]]; then
    add_dir_args+=("--add-dir" "$worktree")
  fi
  env -u TH_JOB_TASK "${worker_env[@]}" setsid nohup "${adopt_timeout[@]}" agy --dangerously-skip-permissions --model "$model" "${add_dir_args[@]}" "$@" --print="$prompt" < /dev/null > agy.log 2>&1 {launch_lock_fd}>&- &
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
  env -u TH_JOB_TASK "${worker_env[@]}" setsid nohup "${worker_cmd[@]}" "$@" -- "$prompt" < /dev/null > claude.log 2>&1 {launch_lock_fd}>&- &
  printf '%s\n' "$!" > "$task_dir/worker.pid"
fi

worker_spawned=1
th_python "$script_dir/th-launch-identity.py" record "$task_dir"
th_python -m team_harness.resource_slots bind "$admission_slot" "$(<"$task_dir/worker.pid")"
admission_slot=""

if [[ $ctx_enabled == 1 ]]; then
  if ! "${context_python[@]}" -m team_harness.launch_prep bind "$ctx_prep_file" "$task_dir"
  then
    exit 1
  fi
fi

(
  flock -x 200
  grep -qxF "$task_dir" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\n' "$task_dir" >> "$watch_dir/tasks.txt"
) 200>"$watch_dir/tasks.lock"

# Independent observational registry; watchd still consumes tasks.txt only.
th_python -m team_harness.status_registry \
  "$task_dir" "$(<"$task_dir/worker.pid")" "$worker_type" "$model" \
  "$task_dir/$worker_type.log" "$journal_workspace" || true

# 상시 감시 데몬: 데몬이 없으면 setsid nohup으로 띄움
if [[ ${TH_NO_DAEMON:-0} != 1 && -x "$script_dir/th-watchd.sh" ]]; then
  touch "$watch_dir/watchd.lock"
  if flock -n "$watch_dir/watchd.lock" true 2>/dev/null; then
    setsid nohup "$script_dir/th-watchd.sh" > /dev/null 2>&1 &
  fi
fi

flock -u "$launch_lock_fd"
exec {launch_lock_fd}>&-
printf 'launched [%s] pid %s → %s\n' "$worker_type" "$(<"$task_dir/worker.pid")" "$task_dir"

inbox_owner="$journal_workspace"
if inbox_waiter_registered "$inbox_owner"; then
  printf '[th-launch] th-inbox-wait 실행 중\n'
fi
