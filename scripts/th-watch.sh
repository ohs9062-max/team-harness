#!/usr/bin/env bash
# th-watch: 등록부의 작업을 조용히 감시하다 사건 하나만 stdout에 내고 끝낸다.
# 정상 진행은 출력하지 않는다. 상태 조회는 th-status.sh를 한 번 실행한다.
# 사용: TH_WATCH_DIR=... TH_STALL_SEC=600 TH_POLL_SEC=30 th-watch.sh [max-wait-sec]
# 작업 규약: 분리된 본 작업 PID는 job.pid, 워커 PID는 worker.pid, 종료는 DONE/FAILED.
set -u

watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
registry="$watch_dir/tasks.txt"
stall_sec=${TH_STALL_SEC:-600}
poll_sec=${TH_POLL_SEC:-30}
max_wait=${1:-21600}
mkdir -p "$watch_dir"
touch "$registry"
case "$poll_sec" in (*[!0-9]*|'') poll_sec=30 ;; esac
(( poll_sec < 1 )) && poll_sec=1
(( poll_sec > 60 )) && poll_sec=60
case "$stall_sec" in (*[!0-9]*|'') stall_sec=600 ;; esac
(( stall_sec < 1 )) && stall_sec=1
case "$max_wait" in (*[!0-9]*|'') max_wait=21600 ;; esac
deadline=$(( $(date +%s) + max_wait ))

pid_alive() { [[ $1 =~ ^[1-9][0-9]*$ ]] && kill -0 "$1" 2>/dev/null; }

cpu_ticks() {
  local pid=$1 proc total=0 ticks
  [[ $pid =~ ^[1-9][0-9]*$ ]] || { echo 0; return; }
  local procs=("$pid")
  local children
  children=$(ps -o pid= --ppid "$pid" 2>/dev/null) || true
  for c in $children; do
    [[ $c =~ ^[1-9][0-9]*$ ]] && procs+=("$c")
  done
  local sid
  sid=$(ps -o sid= -p "$pid" 2>/dev/null | tr -d ' ') || true
  if [[ -n $sid && $sid == "$pid" ]]; then
    while IFS= read -r sproc; do
      [[ $sproc =~ ^[1-9][0-9]*$ ]] && procs+=("$sproc")
    done < <(ps -o pid= -s "$sid" 2>/dev/null)
  fi
  local seen=" "
  for proc in "${procs[@]}"; do
    [[ $seen == *" $proc "* ]] && continue
    seen="$seen$proc "
    ticks=$(awk -F') ' '{split($2, a, " "); print a[12] + a[13]}' "/proc/$proc/stat" 2>/dev/null) || continue
    [[ $ticks =~ ^[0-9]+$ ]] || continue
    total=$(( total + ticks ))
  done
  echo "$total"
}

remove_task() {
  local task=$1 tmp
  tmp=$(mktemp "$watch_dir/tasks.XXXXXX") || return
  awk -v task="$task" '$0 != task' "$registry" > "$tmp" && mv "$tmp" "$registry"
}

notify() {
  local task=$1 message=$2
  printf '%s %s: %s\n' "$(date '+%F %T')" "$task" "$message" >> "$watch_dir/events.log"
  printf '%s: %s\n' "$task" "$message"
  exit 0
}

latest_signature() {
  find "$1" -type f ! -name '.th_*' -printf '%T@\n' 2>/dev/null |
    sort -n | tail -n 1 | cut -d. -f1
}

while (( $(date +%s) < deadline )); do
  while IFS= read -r task || [[ -n $task ]]; do
    [[ -n $task && -d $task ]] || continue
    name=${task#"$HOME"/}
    marker="$task/.th_notified"

    # 1. 완료 판정
    if [[ -f $task/DONE ]]; then
      if [[ ! -f $marker.done ]]; then
        touch "$marker.done"
        remove_task "$task"
        notify "$name" '완료'
      fi
      continue
    fi

    # 2. 실패 판정
    if [[ -f $task/FAILED ]]; then
      if [[ ! -f $marker.failed ]]; then
        touch "$marker.failed"
        remove_task "$task"
        reason=$(tail -c 200 "$task/FAILED" 2>/dev/null | tr '\n' ' ')
        notify "$name" "실패 — $reason"
      fi
      continue
    fi

    # 3. 워커 오류(쿼터·용량) - ERROR: test_x 오탐 방지
    log_file=""
    for cand in "$task/codex.log" "$task/agy.log" "$task/worker.log"; do
      [[ -f $cand ]] || continue
      if grep -iE '^ERROR: ' "$cand" 2>/dev/null | grep -vE '^ERROR: test_' | grep -qiE '(quota|rate limit|usage limit|at capacity|capacity exceeded)'; then
        log_file=$cand
        break
      fi
    done
    if [[ -n $log_file && ! -f $marker.quota ]]; then
      touch "$marker.quota" "$marker.dead"
      line=$(grep -iE '^ERROR: ' "$log_file" | grep -vE '^ERROR: test_' | grep -iE '(quota|rate limit|usage limit|at capacity|capacity exceeded)' | tail -n 1 | cut -c1-180)
      notify "$name" "워커 오류(쿼터·용량) — $line"
    fi

    # 4. 프로세스 상태 확인 (job.pid / worker.pid)
    job_pid=$(cat "$task/job.pid" 2>/dev/null || true)
    worker_pid=$(cat "$task/worker.pid" 2>/dev/null || true)
    alive_pids=()

    if [[ -n $job_pid ]]; then
      if pid_alive "$job_pid"; then
        alive_pids+=("$job_pid")
        pid_alive "$worker_pid" && alive_pids+=("$worker_pid")
      else
        if [[ -f $task/DONE || -f $task/FAILED ]]; then
          continue
        fi
        [[ -f $marker.dead ]] || { touch "$marker.dead"; notify "$name" '중단 — job.pid 프로세스가 사라짐, DONE/FAILED 없음'; }
        continue
      fi
    elif pid_alive "$worker_pid"; then
      alive_pids+=("$worker_pid")
    else
      if [[ -f $task/DONE || -f $task/FAILED ]]; then
        continue
      fi
      [[ -f $marker.dead ]] || { touch "$marker.dead"; notify "$name" '중단 — 실행 중인 프로세스 없음, DONE/FAILED 없음'; }
      continue
    fi

    rm -f "$marker.dead"

    # 5. 멈춤(stall) 판정: CPU와 산출물 파일 무변화
    cpu=0
    for pid in "${alive_pids[@]}"; do cpu=$(( cpu + $(cpu_ticks "$pid") )); done
    newest=$(latest_signature "$task")
    signature="$cpu:$newest"
    state_file="$task/.th_sig"
    previous=""
    [[ -f $state_file ]] && previous=$(<"$state_file")
    now=$(date +%s)

    if [[ ${previous%%|*} != "$signature" ]]; then
      rm -f "$marker.stall"
      printf '%s|%s\n' "$signature" "$now" > "$state_file"
    else
      since=${previous##*|}
      if [[ $since =~ ^[0-9]+$ ]] && (( now - since >= stall_sec )) && [[ ! -f $marker.stall ]]; then
        touch "$marker.stall"
        notify "$name" "멈춤 — CPU·산출물 $(( now - since ))s 변화 없음(프로세스는 살아 있음)"
      fi
    fi
  done < "$registry"
  sleep "$poll_sec"
done
# max-wait은 감시 프로세스 수명만 제한한다. 정상 진행에는 코디네이터를 깨우지 않는다.
exit 0
