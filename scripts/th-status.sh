#!/usr/bin/env bash
# th-status: 요청받았을 때만 등록 작업을 한 번, 한 줄씩 요약한다.
set -u

watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
registry="$watch_dir/tasks.txt"
[[ -f $registry ]] || { echo '등록 작업 없음'; exit 0; }

pid_alive() { [[ $1 =~ ^[1-9][0-9]*$ ]] && kill -0 "$1" 2>/dev/null; }
latest_output() {
  find "$1" -type f ! -name '.th_*' -printf '%T@\n' 2>/dev/null |
    sort -n | tail -n 1 | cut -d. -f1
}

count=0
while IFS= read -r task || [[ -n $task ]]; do
  [[ -n $task && -d $task ]] || continue
  count=$(( count + 1 ))
  state='진행'
  [[ -f $task/DONE ]] && state='완료'
  [[ -f $task/FAILED ]] && state='실패'
  if [[ $state == 진행 ]]; then
    for cand in "$task/codex.log" "$task/agy.log" "$task/worker.log"; do
      [[ -f $cand ]] || continue
      if grep -iE '^ERROR: ' "$cand" 2>/dev/null | grep -vE '^ERROR: test_' | grep -qiE '(quota|rate limit|usage limit|at capacity|capacity exceeded)'; then
        state='워커 오류(쿼터·용량)'
        break
      fi
    done
    if [[ $state == 진행 ]]; then
      job_pid=$(cat "$task/job.pid" 2>/dev/null || true)
      worker_pid=$(cat "$task/worker.pid" 2>/dev/null || true)
      if [[ -n $job_pid ]]; then
        pid_alive "$job_pid" || state='중단?'
      else
        pid_alive "$worker_pid" || state='중단?'
      fi
    fi
  fi
  newest=$(latest_output "$task"); now=$(date +%s)
  [[ $newest =~ ^[0-9]+$ ]] && age=$(( (now - newest) / 60 )) || age='?'
  status=$(tail -n 1 "$task/STATUS" 2>/dev/null | cut -c1-120)
  printf '%s | %s | 마지막 변화 %s분 전 | %s\n' "${task#"$HOME"/}" "$state" "$age" "$status"
done < "$registry"

(( count == 0 )) && echo '등록 작업 없음'
exit 0
