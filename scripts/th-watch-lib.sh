#!/usr/bin/env bash
# th-watch-lib.sh: th-watch 및 th-watchd 공통 감시·검수 함수 라이브러리
# 중복 구현 금지 원칙에 따라 사건 판정 및 검수 로직을 단일 정본으로 유지한다.

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

open_step_detail() {
  local task=$1 program step replay at out action
  program=$(worker_digest_program)
  IFS=$'\t' read -r step replay at out < <(
    PYTHONPATH="$(dirname "$program")/..${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m team_harness.step_records open-steps "$task" | tail -n 1
  ) || true
  [[ -n ${step:-} ]] || return 0
  if [[ $replay == safe ]]; then
    action='다시 실행 가능'
  else
    action='증거 확인 필요, 자동 재실행 금지'
  fi
  printf ' 중단 단계=%s replay=%s → %s' "$step" "$replay" "$action"
}

project_owner() {
  # Worktrees share the main repository's common .git directory, so use its
  # parent as the one coordinator inbox for the whole project.
  if [[ -n ${TH_INBOX_OWNER:-} ]]; then
    printf '%s\n' "$TH_INBOX_OWNER"
    return
  fi
  local directory=$1 common
  common=$(git -C "$directory" rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || {
    printf '%s\n' "$directory"
    return
  }
  if [[ $(basename "$common") == .git ]]; then
    dirname "$common"
  else
    printf '%s\n' "$directory"
  fi
}

# Launch ownership follows the task rather than the shell that happened to
# invoke th-launch.  Keep this separate from project_owner(): the latter folds
# linked worktrees into their shared coordinator inbox after ownership has been
# chosen.
resolve_launch_owner() {
  local task_dir=$1 worktree=$2 fallback=${3:-$PWD} candidate top
  for candidate in "$task_dir" "$worktree"; do
    [[ -n $candidate && -d $candidate ]] || continue
    top=$(git -C "$candidate" rev-parse --show-toplevel 2>/dev/null) || continue
    realpath "$top"
    return 0
  done
  realpath "$fallback"
}

warn_launch_owner_mismatch() {
  local owner=$1 pwd_owner=$2
  [[ -n $pwd_owner && $owner != "$pwd_owner" ]] || return 0
  printf '[th-launch] 경고: owner=%s (PWD 저장소 %s와 다름)\n' "$owner" "$pwd_owner" >&2
}

is_inbox_wait_pid() {
  local pid=$1 cmdline
  [[ $pid =~ ^[1-9][0-9]*$ && -r /proc/$pid/cmdline ]] || return 1
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [[ $cmdline == *th-inbox-wait.sh* ]] && kill -0 "$pid" 2>/dev/null
}

cleanup_inbox_waiter_registry() {
  local owner=$1 registry entry pid
  registry="$owner/.claude/.inbox_wait.d"
  [[ -d $registry ]] || return 0
  for entry in "$registry"/*; do
    [[ -f $entry ]] || continue
    pid=$(basename "$entry")
    is_inbox_wait_pid "$pid" || rm -f "$entry"
  done
}

inbox_waiter_registered() {
  local owner=$1 registry entry pid legacy
  registry="$owner/.claude/.inbox_wait.d"
  cleanup_inbox_waiter_registry "$owner"
  for entry in "$registry"/*; do
    [[ -f $entry ]] || continue
    pid=$(basename "$entry")
    is_inbox_wait_pid "$pid" && return 0
  done
  legacy="$owner/.claude/.inbox_wait.pid"
  [[ -r $legacy ]] || return 1
  pid=$(<"$legacy")
  is_inbox_wait_pid "$pid"
}

log_is_from_current_worker() {
  local task=$1 log=$2 pid_file="$task/worker.pid"
  [[ ! -f $pid_file || ! $log -ot $pid_file ]]
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
      lower = tolower($0)
      # The suffix is also normally echoed inside the initial worker prompt;
      # never turn its own fail-fast instruction into a quota lock.
      if (lower ~ /^[-*[:space:]]*quota\/permission\/license error[[:space:]]*→[[:space:]]*`?failed`?/) next
      if ($0 ~ /^[[:space:]]*([-*+#]|>|[0-9]+[.)])[[:space:]]/) next
      if ($0 ~ /^[+-][^+-]/ || $0 ~ /^@@ /) next
      if ($0 ~ /^ERROR:[[:space:]]*test_/) next
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
  PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" uv run --directory "$lib_dir/.." python -m team_harness.watch_inspect "$task"
}

status_failure_reason() {
  local status_file=$1 line
  [[ -f $status_file ]] || return 0
  line=$(tail -n 1 "$status_file" 2>/dev/null) || return 0
  # Keep the status content, not its timestamp, and cap by characters so UTF-8
  # notices remain readable.
  python3 - "$line" <<'PY'
import re
import sys

line = sys.argv[1].strip()
line = re.sub(
    r"^\[?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
    r"(?:Z|[+-]\d{2}:\d{2}| KST)?\]?[:\s|]*",
    "",
    line,
)
line = re.sub(r"^\[?\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?\]?[:\s|]*", "", line)
print(line[:200], end="")
PY
}

status_last_start_epoch() {
  # A later START is evidence that a worker already handed work to its next
  # step.  Treat unparseable or legacy STATUS lines as no evidence.
  local status_file=$1 line timestamp
  [[ -f $status_file ]] || return 0
  line=$(grep 'START step=' "$status_file" 2>/dev/null | tail -n 1) || return 0
  [[ $line =~ ^([0-9]{4}-[0-9]{2}-[0-9]{2}[[:space:]T][0-9]{2}:[0-9]{2}(:[0-9]{2})?([[:space:]Z]|[+-][0-9]{2}:[0-9]{2})?) ]] || return 0
  timestamp=${BASH_REMATCH[1]}
  date -d "$timestamp" +%s 2>/dev/null || true
}

watch_event_log() {
  local watch_dir=$1 task=$2 detail=$3
  printf '%s [인계 대기] %s: %s\n' "$(date '+%F %T')" "$task" "$detail" >> "$watch_dir/events.log"
}

safe_step_auto_retry() {
  # The interactive watcher only reports.  The daemon alone may relaunch a
  # bounded, explicitly safe step, and never changes its worker settings.
  local task=$1 watch_dir=$2 marker=$3 callback_fn=$4 owner=$5
  [[ ${TH_AUTO_RETRY:-1} != 0 ]] || return 1

  local program py_src
  program=$(worker_digest_program)
  py_src=$(cd "$(dirname "$program")/.." && pwd)
  local -a open_steps=()
  mapfile -t open_steps < <(
    PYTHONPATH="$py_src${PYTHONPATH:+:$PYTHONPATH}" \
      python3 -m team_harness.step_records open-steps "$task"
  )
  (( ${#open_steps[@]} > 0 )) || return 1

  local open step replay at out
  for open in "${open_steps[@]}"; do
    IFS=$'\t' read -r step replay at out <<< "$open"
    [[ $replay == safe ]] || return 1
  done
  IFS=$'\t' read -r step replay at out <<< "${open_steps[-1]}"

  local usage="$watch_dir/usage.jsonl" launch_meta
  [[ -f $usage && -f $task/.th_worktree ]] || return 1
  launch_meta=$(python3 - "$usage" "$task" <<'PY'
import json
import sys
from pathlib import Path

ledger, task = map(Path, sys.argv[1:])
target = task.resolve()
records = []
for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    if not isinstance(row, dict) or row.get("v") != 1:
        continue
    try:
        matched = Path(str(row.get("task", ""))).expanduser().resolve() == target
    except OSError:
        matched = False
    if matched:
        records.append(row)
if records:
    row = records[-1]
    values = (row.get("worker"), row.get("model"), row.get("effort"))
    if all(isinstance(value, str) and value for value in values):
        print("\t".join(values))
PY
)
  [[ -n $launch_meta ]] || return 1

  local worker model effort
  IFS=$'\t' read -r worker model effort <<< "$launch_meta"
  [[ -f $watch_dir/quota_lock.$worker ]] && return 1

  local max_retries=${TH_MAX_RETRIES:-2}
  [[ $max_retries =~ ^[0-9]+$ ]] || max_retries=2
  local attempts
  attempts=$(python3 - "$usage" "$task" <<'PY'
import json
import sys
from pathlib import Path

ledger, task = map(Path, sys.argv[1:])
target = task.resolve()
count = 0
for line in ledger.read_text(encoding="utf-8", errors="replace").splitlines():
    try:
        row = json.loads(line)
        if isinstance(row, dict) and row.get("v") == 1 and Path(str(row.get("task", ""))).expanduser().resolve() == target:
            count += 1
    except (OSError, json.JSONDecodeError):
        continue
print(count)
PY
)
  [[ $attempts =~ ^[0-9]+$ ]] || return 1
  (( attempts < max_retries + 1 )) || return 1

  local auto_log="$watch_dir/auto_retries.tsv" last_auto now_epoch
  last_auto=$(awk -F $'\t' -v task="$task" '$2 == task { value=$1 } END { print value }' "$auto_log" 2>/dev/null || true)
  now_epoch=$(date +%s)
  if [[ $last_auto =~ ^[0-9]+$ ]] && (( now_epoch - last_auto < 600 )); then
    return 1
  fi

  local worktree launch_script retry_no retry_output rc
  worktree=$(<"$task/.th_worktree")
  [[ -n $worktree ]] || return 1
  launch_script="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/th-launch.sh"
  retry_no=$attempts
  retry_output=$(TH_NO_DAEMON=1 "$launch_script" "$task" "$worker" "$model" "$effort" "$worktree" 2>&1) || rc=$?
  if [[ ${rc:-0} -eq 0 ]]; then
    printf '%s\t%s\t%s\n' "$now_epoch" "$task" "$step" >> "$auto_log"
    touch "$marker.dead"
    "$callback_fn" "$task" "자동 재시도" "자동 재시도 $retry_no/$max_retries — 단계=$step" "-" "$owner"
    return 0
  fi
  "$callback_fn" "$task" "자동 재시도 실패" "자동 재시도 실패 — ${retry_output:0:200}" "-" "$owner"
  return 0
}

inspect_task_event() {
  local task=$1 watch_dir=$2 marker_prefix=$3 stall_sec=$4 target_workspace=$5 callback_fn=$6
  [[ -n $task && -d $task ]] || return 0
  local marker="$task/$marker_prefix"

  local owner owner_source
  owner=$(cat "$task/.th_owner" 2>/dev/null || true)
  if [[ -n $owner ]]; then
    owner=$(project_owner "$owner")
  elif [[ -f $task/.th_worktree ]]; then
    owner_source=$(<"$task/.th_worktree")
    owner=$(project_owner "$owner_source")
  elif [[ -n $target_workspace ]]; then
    local journal_log="$target_workspace/.claude/work_log.md"
    local journal_logs=("$(dirname "$journal_log")"/work_log*.md)
    if (( ${#journal_logs[@]} > 0 )) && grep -qsF "| $task |" "${journal_logs[@]}"; then
      owner=$(project_owner "$target_workspace")
    else
      owner="(unknown)"
    fi
  fi

  # target_workspace가 지정되어 있고 본인 소유가 아닌 경우:
  # 쿼터 잠금만 생성하고 사건 통지는 건너뜀
  local target_owner=""
  [[ -n $target_workspace ]] && target_owner=$(project_owner "$target_workspace")
  if [[ -n $target_owner && -n $owner && $owner != "$target_owner" ]]; then
    for cand in "$task/codex.log" "$task/agy.log" "$task/claude.log"; do
      [[ -f $cand ]] || continue
      log_is_from_current_worker "$task" "$cand" || continue
      local lock="$watch_dir/quota_lock.$(basename "$cand" .log)"
      [[ -f $lock || -f $task/DONE || -f $task/FAILED ]] && continue
      local qline
      qline=$(quota_error_line "$cand")
      if [[ -n $qline ]]; then
        if [[ ${qline,,} == *"at capacity"* || ${qline,,} == *"capacity exceeded"* ]]; then
          printf '%s | %s | %s | expires=%s\n' "$(date '+%F %R')" "$task" "$qline" "$(( $(date +%s) + 900 ))" > "$lock"
        else
          printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$qline" > "$lock"
        fi
      fi
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

  # A resumed worker or replacement detached job cancels any earlier handoff
  # wait.  Do this before event routing because a live replacement does not
  # itself emit job_completed.
  local current_worker_pid current_job_pid current_worker_alive=0
  current_worker_pid=$(cat "$task/worker.pid" 2>/dev/null || true)
  current_job_pid=$(cat "$task/job.pid" 2>/dev/null || true)
  if [[ $current_worker_pid =~ ^[1-9][0-9]*$ ]] && kill -0 "$current_worker_pid" 2>/dev/null; then
    current_worker_alive=1
  fi
  if (( current_worker_alive )) || [[ $distinct_job_alive == 1 ]]; then
    rm -f "$marker".handoff_wait.*
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
    # A worker may rewrite DONE while finishing its report.  Its presence,
    # rather than its mtime, defines this attempt's completion event; th-launch
    # removes .th_* markers before a genuinely new attempt.
    if [[ ! -f $marker.done ]]; then
      rm -f "$marker.failed"
      touch "$marker.done"
      local v_res
      if [[ $marker_prefix == .th_daemon ]]; then
        [[ -f $task/verify.sh ]] && v_res="대기" || v_res="없음"
      else
        v_res=$(run_verification "$task")
      fi
      "$callback_fn" "$task" "완료" "완료" "$v_res" "$owner"
    fi
    rm -f "$marker".handoff_wait.*
    return 0
  fi

  # 2. 실패 판정
  if [[ $terminal == FAILED ]]; then
    # Keep the STATUS-only → concrete FAILED-file escalation: it supplies a
    # real failure reason after an earlier provisional status notice.
    if [[ ! -f $marker.failed || $failed_file -nt $marker.failed ]]; then
      rm -f "$marker.done"
      touch "$marker.failed"
      local reason
      if [[ -f $failed_file ]]; then
        reason=$(tail -n 5 "$failed_file" 2>/dev/null | tr '\n' ' ')
      else
        reason=$(status_failure_reason "$task/STATUS")
      fi
      "$callback_fn" "$task" "실패" "실패 — $reason" "-" "$owner"
    fi
    rm -f "$marker".handoff_wait.*
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
    # A detached job ending is normally a handoff, not an actionable event.
    # Only wake the coordinator when no worker has resumed it for the full
    # grace period.  Markers are keyed by pid so a replacement job is fresh.
    local worker_pid worker_alive=0 job_pid now_epoch wait_marker wait_since
    worker_pid=$(cat "$task/worker.pid" 2>/dev/null || true)
    job_pid=$(cat "$task/job.pid" 2>/dev/null || true)
    if [[ $worker_pid =~ ^[1-9][0-9]*$ ]] && kill -0 "$worker_pid" 2>/dev/null; then
      worker_alive=1
    fi
    if (( worker_alive )); then
      rm -f "$marker.handoff_wait.${job_pid:-unknown}"
      watch_event_log "$watch_dir" "$task" "job.pid(${job_pid:-?}) 종료, worker 생존 — 인계 진행 중"
      return 0
    fi

    # Preserve TH-D36: the daemon gets the first chance to relaunch an open
    # safe step; a successful retry must never also produce a handoff alert.
    if [[ $marker_prefix == .th_daemon ]] && safe_step_auto_retry "$task" "$watch_dir" "$marker" "$callback_fn" "$owner"; then
      rm -f "$marker.handoff_wait.${job_pid:-unknown}"
      watch_event_log "$watch_dir" "$task" "job.pid(${job_pid:-?}) 종료, safe 단계 자동 재시도"
      return 0
    fi

    now_epoch=$(date +%s)
    wait_marker="$marker.handoff_wait.${job_pid:-unknown}"
    if [[ -f $wait_marker ]]; then
      wait_since=$(<"$wait_marker")
    else
      wait_since=$now_epoch
      printf '%s\n' "$wait_since" > "$wait_marker"
    fi
    if [[ ! $wait_since =~ ^[0-9]+$ ]]; then
      wait_since=$now_epoch
      printf '%s\n' "$wait_since" > "$wait_marker"
    fi
    local last_start
    last_start=$(status_last_start_epoch "$task/STATUS")
    if [[ $last_start =~ ^[0-9]+$ ]] && (( last_start > wait_since )); then
      rm -f "$wait_marker"
      watch_event_log "$watch_dir" "$task" "job.pid(${job_pid:-?}) 종료 뒤 새 START 확인 — 인계 완료"
      return 0
    fi
    local handoff_min=${TH_HANDOFF_STALL_MIN:-5}
    case "$handoff_min" in (*[!0-9]*|'') handoff_min=5 ;; esac
    local handoff_sec=$(( handoff_min * 60 ))
    if (( now_epoch - wait_since < handoff_sec )); then
      watch_event_log "$watch_dir" "$task" "job.pid(${job_pid:-?}) 종료, worker 없음 — 인계 대기 $(( now_epoch - wait_since ))/${handoff_sec}s"
      return 0
    fi
    local handoff_marker="$marker.handoff_stall.${job_pid:-unknown}"
    if [[ ! -f $handoff_marker ]]; then
      touch "$handoff_marker"
      local status_detail
      status_detail=$(status_failure_reason "$task/STATUS")
      "$callback_fn" "$task" "워커 중단(인계 없음)" "워커 중단(인계 없음) — job.pid(${job_pid:-?}) 종료, DONE/FAILED 없음; STATUS: ${status_detail:-없음}$(open_step_detail "$task")" "-" "$owner"
    else
      watch_event_log "$watch_dir" "$task" "job.pid(${job_pid:-?}) 인계 없음 사건은 이미 통지됨"
    fi
    return 0
  fi

  # 3. 워커 오류(쿼터·용량)
  local log_file=""
  for cand in "$task/codex.log" "$task/agy.log" "$task/claude.log" "$task/worker.log"; do
    [[ -f $cand ]] || continue
    log_is_from_current_worker "$task" "$cand" || continue
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
    if [[ ${line,,} == *"at capacity"* || ${line,,} == *"capacity exceeded"* ]]; then
      printf '%s | %s | %s | expires=%s\n' "$(date '+%F %R')" "$task" "$line" "$(( $(date +%s) + 900 ))" > "$watch_dir/quota_lock.$lock_type"
    else
      printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$line" > "$watch_dir/quota_lock.$lock_type"
    fi
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
    if [[ $marker_prefix == .th_daemon ]] && safe_step_auto_retry "$task" "$watch_dir" "$marker" "$callback_fn" "$owner"; then
      return 0
    fi
    if [[ ! -f $marker.dead ]]; then
      touch "$marker.dead"
      "$callback_fn" "$task" "중단" "중단 — 실행 중인 프로세스 없음, DONE/FAILED 없음$(open_step_detail "$task")" "-" "$owner"
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
