#!/usr/bin/env bash
# th-watchd: 상시 감시 데몬 (단일 인스턴스, inbox 기록, 데스크톱 알림, events.log)
# 사건 판정 로직은 scripts/th-watch-lib.sh와 공유한다 (중복 구현 금지).
set -u
shopt -s nullglob

watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
registry="$watch_dir/tasks.txt"
stall_sec=${TH_STALL_SEC:-600}
poll_sec=${TH_POLL_SEC:-10}

mkdir -p "$watch_dir"
touch "$registry"

case "$poll_sec" in (*[!0-9]*|'') poll_sec=10 ;; esac
(( poll_sec < 1 )) && poll_sec=1
case "$stall_sec" in (*[!0-9]*|'') stall_sec=600 ;; esac
(( stall_sec < 1 )) && stall_sec=1

# 단일 인스턴스 보장 (flock)
lock_file="$watch_dir/watchd.lock"
exec 201>"$lock_file"
if ! flock -n 201; then
  echo "th-watchd is already running." >&2
  exit 0
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
self_path=$(realpath "${BASH_SOURCE[0]}")
lib_path=$(realpath "$script_dir/th-watch-lib.sh")

# shellcheck source=scripts/th-watch-lib.sh
source "$lib_path"

get_script_sig() {
  stat -c '%Y-%i' "$self_path" "$lib_path" 2>/dev/null || true
}
last_sig=$(get_script_sig)

handle_daemon_event() {
  local task=$1 event_type=$2 summary=$3 v_res=$4 owner=$5
  local name=${task#"$HOME"/}
  local now_time
  now_time=$(date '+%F %R')
  local now_sec
  now_sec=$(date '+%F %T')

  # 1. 소유 작업 공간의 .claude/inbox.md에 한 줄 append
  if [[ -n $owner && -d $owner ]]; then
    local inbox_file="$owner/.claude/inbox.md"
    mkdir -p "$owner/.claude"
    touch "$inbox_file"
    printf '%s | %s | %s | %s | %s\n' "$now_time" "$name" "$event_type" "$summary" "$v_res" >> "$inbox_file"
  fi

  # 2. notify-send 데스크톱 알림 (있으면 실행, 없으면 건너뜀)
  if command -v notify-send >/dev/null 2>&1; then
    local notif_body="$name: $summary"
    [[ $v_res != "-" ]] && notif_body="$notif_body (검수: $v_res)"
    notify-send "team-harness: $event_type" "$notif_body" 2>/dev/null || true
  fi

  # 3. events.log 기록
  printf '%s [%s] %s: %s (검수: %s)\n' "$now_sec" "$event_type" "$name" "$summary" "$v_res" >> "$watch_dir/events.log"

  # C. Jev 관찰 (watch_cli.py 호출)
  if [[ ${TH_JEV_WATCH:-on} != "off" ]]; then
    python3 -m team_harness.decisions.watch_cli "$task" "$event_type" "$watch_dir" 2>/dev/null || true
  fi
}

while true; do
  # 자기 스크립트 또는 라이브러리 파일 변경 시 스스로 재실행 (exec)
  current_sig=$(get_script_sig)
  if [[ -n $current_sig && -n $last_sig && $current_sig != "$last_sig" ]]; then
    echo "th-watchd: script updated, restarting via exec..." >&2
    exec "$self_path" "$@"
  fi

  if [[ -f $registry ]]; then
    while IFS= read -r task || [[ -n $task ]]; do
      [[ -n $task && -d $task ]] || continue
      inspect_task_event "$task" "$watch_dir" ".th_daemon" "$stall_sec" "" handle_daemon_event
    done < "$registry"
  fi

  if [[ ${TH_ONCE:-0} == 1 ]]; then
    break
  fi

  sleep "$poll_sec"
done

exit 0
