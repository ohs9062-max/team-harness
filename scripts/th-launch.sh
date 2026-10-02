#!/usr/bin/env bash
# th-launch: 최신 worker_suffix.md를 반드시 덧붙여 Codex 워커를 분리 실행하고 등록한다.
# 사용: th-launch.sh <작업폴더> <모델> <effort> <작업트리> [codex exec 추가 인자]
# 정책 변경은 다음 launch부터 적용하며 실행 중 워커는 교체하지 않는다.
set -euo pipefail
(( $# >= 4 )) || { echo '사용: th-launch.sh <작업폴더> <모델> <effort> <작업트리> [추가 인자]'; exit 2; }
task_dir=$(realpath "$1"); model=$2; effort=$3; worktree=$4; shift 4
watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
# 동기화된 전역 사본만 사용한다. 임의의 오래된 경로를 override하지 않는다.
suffix=${TH_WORKER_SUFFIX:-"$HOME/.team-harness/worker_suffix.md"}
[[ -f $task_dir/task.md ]] || { echo "task.md 없음: $task_dir"; exit 1; }
[[ -f $suffix ]] || { echo "worker_suffix.md 없음(최신 정책을 붙일 수 없음): $suffix"; exit 1; }
mkdir -p "$watch_dir"; touch "$watch_dir/tasks.txt"
rm -f "$task_dir"/DONE "$task_dir"/FAILED "$task_dir"/.th_* "$task_dir"/job.pid
prompt="$(<"$task_dir/task.md")

---
$(<"$suffix")
- 결과 폴더: $task_dir. 분리 실행한 본 작업의 PID를 $task_dir/job.pid에 기록할 것. 끝나면 DONE, 실패 시 FAILED+이유를 남길 것."
cd "$task_dir"
setsid nohup codex exec -m "$model" -c "model_reasoning_effort=$effort" --dangerously-bypass-approvals-and-sandbox -C "$worktree" "$@" "$prompt" < /dev/null > codex.log 2>&1 &
printf '%s\n' "$!" > "$task_dir/worker.pid"
grep -qxF "$task_dir" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\n' "$task_dir" >> "$watch_dir/tasks.txt"
printf 'launched pid %s → %s\n' "$(<"$task_dir/worker.pid")" "$task_dir"
