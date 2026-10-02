#!/usr/bin/env bash
# th-launch: 최신 worker_suffix.md를 반드시 덧붙여 워커(codex|agy)를 분리 실행하고 등록한다.
# 사용: th-launch.sh <작업폴더> [codex|agy] <모델> <effort> <작업트리> [추가 인자]
#       (워커 생략 시 기본값: codex)
# 정책 변경은 다음 launch부터 적용하며 실행 중 워커는 교체하지 않는다.
set -euo pipefail

journal_workspace=${TH_WORKSPACE:-}
if [[ -z $journal_workspace ]]; then
  journal_workspace=$(git -C "$PWD" rev-parse --show-toplevel 2>/dev/null || printf '%s' "$PWD")
fi
journal_workspace=$(realpath "$journal_workspace")
journal_log="$journal_workspace/.claude/work_log.md"
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
  printf '%s | 지시 | %s | worker=%s model=%s task.md=%s | %s\n' \
    "$(date '+%F %R')" "$task_dir" "$worker_type" "$model" "$task_dir/task.md" "$journal_workspace" >> "$journal_log"
}

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
journal_append
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
      exe=""
      IFS= read -r -d '' exe < "$p" || true
      [[ ${exe##*/} == "agy" ]] || continue
      cmd=$(tr '\0' ' ' < "$p" 2>/dev/null || true)
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
fi

grep -qxF "$task_dir" "$watch_dir/tasks.txt" 2>/dev/null || printf '%s\n' "$task_dir" >> "$watch_dir/tasks.txt"
printf 'launched [%s] pid %s → %s\n' "$worker_type" "$(<"$task_dir/worker.pid")" "$task_dir"
