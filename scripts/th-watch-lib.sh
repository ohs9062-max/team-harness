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
  find "$1" -maxdepth 3 -type f ! -name '.th_*' \
    \( -name STATUS -o -name WORKLOG.md -o -name '*.log' \) -printf '%T@\n' 2>/dev/null |
    sort -n | tail -n 1 | cut -d. -f1
}

run_verification() {
  local task=$1
  local verify_sh="$task/verify.sh"
  local verified_file="$task/VERIFIED"
  local failed_file="$task/VERIFY_FAILED"

  if [[ -f $verified_file ]]; then
    echo "통과"
    return 0
  fi
  if [[ -f $failed_file ]]; then
    echo "실패"
    return 0
  fi
  if [[ ! -f $verify_sh ]]; then
    echo "없음"
    return 0
  fi

  local worktree
  worktree=$(cat "$task/.th_worktree" 2>/dev/null || true)
  [[ -n $worktree && -d $worktree ]] || worktree="$task"

  local timeout_sec=${TH_VERIFY_TIMEOUT:-600}
  local verify_out_file
  verify_out_file=$(mktemp) || verify_out_file="$task/.verify.tmp"

  local verify_code=0
  (
    cd "$worktree"
    timeout "$timeout_sec" bash "$verify_sh"
  ) > "$verify_out_file" 2>&1 || verify_code=$?

  local failed_items=()
  local warnings=()

  if (( verify_code != 0 )); then
    if (( verify_code == 124 )); then
      failed_items+=("verify.sh 타임아웃 (${timeout_sec}s 초과)")
    else
      failed_items+=("verify.sh 실패 (종료 코드 $verify_code)")
    fi
  fi

  # 공통 검사: git 저장소인 경우
  local repo_root
  repo_root=$(git -C "$worktree" rev-parse --show-toplevel 2>/dev/null || true)
  if [[ -n $repo_root && -d $repo_root ]]; then
    # 1. 저장소 루트 새 *.md / RESULTS.md / WORKLOG.md 검사
    while IFS= read -r line; do
      [[ -n $line ]] || continue
      local status_code="${line:0:2}"
      local file_path="${line:3}"
      if [[ $status_code == "??" && $file_path != */* ]]; then
        if [[ $file_path == *.md || $file_path == "RESULTS.md" || $file_path == "WORKLOG.md" ]]; then
          failed_items+=("저장소 루트 새 마크다운 파일 생성 금지 위반: $file_path")
        fi
      fi
    done < <(git -C "$repo_root" status --porcelain 2>/dev/null || true)

    # 2. uv.lock, pyproject.toml 변경 확인
    local pyproject_changed=0 uv_lock_changed=0
    while IFS= read -r diff_file; do
      [[ -n $diff_file ]] || continue
      local base_name
      base_name=$(basename "$diff_file")
      if [[ $base_name == "pyproject.toml" ]]; then
        pyproject_changed=1
      elif [[ $base_name == "uv.lock" ]]; then
        uv_lock_changed=1
      fi
    done < <(git -C "$worktree" status --porcelain 2>/dev/null | awk '{print $NF}' || true)

    if (( pyproject_changed != 0 )); then
      failed_items+=("pyproject.toml 변경 감지 (검토 필요)")
    fi
    if (( uv_lock_changed != 0 )); then
      warnings+=("검토 필요: uv.lock 변경 감지")
    fi
  fi

  if (( ${#failed_items[@]} > 0 )); then
    {
      printf '=== 검수 실패 항목 ===\n'
      for item in "${failed_items[@]}"; do
        printf -- '- %s\n' "$item"
      done
      printf '\n=== verify.sh 출력 (끝 40줄) ===\n'
      tail -n 40 "$verify_out_file"
    } > "$failed_file"
    rm -f "$verify_out_file"
    echo "실패"
    return 0
  else
    {
      printf '=== 검수 통과 ===\n'
      if (( ${#warnings[@]} > 0 )); then
        printf '=== 경고 (검토 필요) ===\n'
        for w in "${warnings[@]}"; do
          printf -- '- %s\n' "$w"
        done
      fi
      printf '\n=== verify.sh 출력 (끝 40줄) ===\n'
      tail -n 40 "$verify_out_file"
    } > "$verified_file"
    rm -f "$verify_out_file"
    echo "통과"
    return 0
  fi
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
      qline=$(grep -iE '^ERROR: ' "$cand" 2>/dev/null | grep -vE '^ERROR: test_' | grep -iE '(quota|rate limit|usage limit|at capacity|capacity exceeded)' | tail -n 1 | cut -c1-180)
      [[ -n $qline ]] && printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$qline" > "$lock"
    done
    return 0
  fi

  # 1. 완료 판정
  if [[ -f $task/DONE ]]; then
    if [[ ! -f $marker.done ]]; then
      touch "$marker.done"
      local v_res
      v_res=$(run_verification "$task")
      "$callback_fn" "$task" "완료" "완료" "$v_res" "$owner"
    fi
    return 0
  fi

  # 2. 실패 판정
  if [[ -f $task/FAILED ]]; then
    if [[ ! -f $marker.failed ]]; then
      touch "$marker.failed"
      local reason
      reason=$(tail -n 5 "$task/FAILED" 2>/dev/null | tr '\n' ' ')
      "$callback_fn" "$task" "실패" "실패 — $reason" "-" "$owner"
    fi
    return 0
  fi

  # 3. 워커 오류(쿼터·용량)
  local log_file=""
  for cand in "$task/codex.log" "$task/agy.log" "$task/claude.log" "$task/worker.log"; do
    [[ -f $cand ]] || continue
    if grep -iE '^ERROR: ' "$cand" 2>/dev/null | grep -vE '^ERROR: test_' | grep -qiE '(quota|rate limit|usage limit|at capacity|capacity exceeded)'; then
      log_file=$cand
      break
    fi
  done
  if [[ -n $log_file && ! -f $marker.quota ]]; then
    touch "$marker.quota" "$marker.dead"
    local line
    line=$(grep -iE '^ERROR: ' "$log_file" | grep -vE '^ERROR: test_' | grep -iE '(quota|rate limit|usage limit|at capacity|capacity exceeded)' | tail -n 1 | cut -c1-180)
    local lock_type
    lock_type=$(basename "$log_file" .log)
    [[ $lock_type == worker ]] && lock_type=unknown
    printf '%s | %s | %s\n' "$(date '+%F %R')" "$task" "$line" > "$watch_dir/quota_lock.$lock_type"
    "$callback_fn" "$task" "워커 오류(쿼터·용량)" "워커 오류(쿼터·용량) — $line (새 $lock_type 실행 잠금: $watch_dir/quota_lock.$lock_type)" "-" "$owner"
    return 0
  fi

  # 4. 프로세스 상태 확인 (job.pid / worker.pid)
  local job_pid worker_pid
  job_pid=$(cat "$task/job.pid" 2>/dev/null || true)
  worker_pid=$(cat "$task/worker.pid" 2>/dev/null || true)
  local alive_pids=()

  pid_alive "$job_pid" && alive_pids+=("$job_pid")
  pid_alive "$worker_pid" && alive_pids+=("$worker_pid")

  if (( ${#alive_pids[@]} == 0 )); then
    if [[ -f $task/DONE || -f $task/FAILED ]]; then
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
