#!/usr/bin/env bash
# th-launch: 최신 worker_suffix.md를 반드시 덧붙여 워커(codex|agy)를 분리 실행하고 등록한다.
# 사용: th-launch.sh <작업폴더> [codex|agy] <모델> <effort> <작업트리> [추가 인자]
#       (워커 생략 시 기본값: codex)
# 정책 변경은 다음 launch부터 적용하며 실행 중 워커는 교체하지 않는다.
set -euo pipefail

(( $# >= 1 )) || { echo '사용: th-launch.sh <작업폴더> [codex|agy] <모델> <effort> <작업트리> [추가 인자]'; exit 2; }
task_dir=$(realpath "$1"); shift

worker_type="codex"
if (( $# >= 1 )) && [[ "$1" == "codex" || "$1" == "agy" ]]; then
  worker_type="$1"
  shift
fi

if [[ "$worker_type" == "codex" ]]; then
  (( $# >= 3 )) || { echo '사용: th-launch.sh <작업폴더> [codex] <모델> <effort> <작업트리> [codex 추가 인자]'; exit 2; }
  model=$1; effort=$2; worktree=$3; shift 3
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
fi

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

if [[ "$worker_type" == "codex" ]]; then
  setsid nohup codex exec -m "$model" -c "model_reasoning_effort=$effort" --dangerously-bypass-approvals-and-sandbox -C "$worktree" "$@" "$prompt" < /dev/null > codex.log 2>&1 &
  printf '%s\n' "$!" > "$task_dir/worker.pid"
elif [[ "$worker_type" == "agy" ]]; then
  add_dir_args=("--add-dir" "$task_dir")
  if [[ -n "$worktree" && "$worktree" != "$task_dir" && -d "$worktree" ]]; then
    add_dir_args+=("--add-dir" "$worktree")
  fi
  setsid nohup agy --dangerously-skip-permissions --model "$model" "${add_dir_args[@]}" "$@" --print="$prompt" < /dev/null > agy.log 2>&1 &
  init_pid=$!
  sleep 3

  # 재부모화된 실제 agy PID 찾기 (같은 결과 폴더 인자를 가진 agy 프로세스)
  actual_pid=""
  for p in /proc/[0-9]*/cmdline; do
    cmd=$(tr '\0' ' ' < "$p" 2>/dev/null || true)
    if [[ "$cmd" =~ (^|[[:space:]/])agy([[:space:]]|$) ]] && echo "$cmd" | grep -Fq "$task_dir"; then
      pid=$(basename "$(dirname "$p")")
      if [[ "$pid" != "$$" ]]; then
        actual_pid="$pid"
        break
      fi
    fi
  done

  if [[ -n "$actual_pid" ]]; then
    printf '%s\n' "$actual_pid" > "$task_dir/worker.pid"
  else
    printf '%s\n' "$init_pid" > "$task_dir/worker.pid"
  fi
fi

grep -qxF "$task_dir" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\n' "$task_dir" >> "$watch_dir/tasks.txt"
printf 'launched [%s] pid %s → %s\n' "$worker_type" "$(<"$task_dir/worker.pid")" "$task_dir"
