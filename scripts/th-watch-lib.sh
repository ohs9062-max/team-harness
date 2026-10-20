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
    if [[ ! -f $marker.done || $task/DONE -nt $marker.done ]]; then
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
    return 0
  fi

  # 2. 실패 판정
  if [[ $terminal == FAILED ]]; then
    if [[ ! -f $marker.failed || $failed_file -nt $marker.failed ]]; then
      rm -f "$marker.done"
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
