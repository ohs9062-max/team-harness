#!/usr/bin/env bash
# th-watchd: 상시 감시 데몬 (단일 인스턴스, inbox 기록, 데스크톱 알림, events.log)
# 사건 판정 로직은 scripts/th-watch-lib.sh와 공유한다 (중복 구현 금지).
set -u
shopt -s nullglob

watch_dir=${TH_WATCH_DIR:-"$HOME/.team-harness/watch"}
registry="$watch_dir/tasks.txt"
stall_sec=${TH_STALL_SEC:-600}
poll_sec=${TH_POLL_SEC:-10}
inbox_unread_sec=${TH_INBOX_UNREAD_SEC:-600}

mkdir -p "$watch_dir"
touch "$registry"

case "$poll_sec" in (*[!0-9]*|'') poll_sec=10 ;; esac
(( poll_sec < 1 )) && poll_sec=1
case "$stall_sec" in (*[!0-9]*|'') stall_sec=600 ;; esac
(( stall_sec < 1 )) && stall_sec=1
case "$inbox_unread_sec" in (*[!0-9]*|'') inbox_unread_sec=600 ;; esac
(( inbox_unread_sec < 1 )) && inbox_unread_sec=600

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
digest_jobs=()
queue_digest() {
  local task=$1 event=$2 event_id
  event_id=$(stat -c '%y-%i' "$task/.th_daemon.$event" 2>/dev/null || printf '%s' "$event")
  # Close the daemon singleton descriptor and detach all standard streams:
  # digest must never keep watchd.lock or a caller's capture pipe open.
  "$script_dir/th-digest.sh" "$task" --event "$event" --event-id "$event_id" \
    201>&- </dev/null >>"$watch_dir/digest.log" 2>&1 &
  digest_jobs+=("$!")
}

stop_digest_jobs() {
  trap - TERM INT EXIT
  for digest_pid in "${digest_jobs[@]}"; do
    kill -TERM "$digest_pid" 2>/dev/null || true
  done
  for digest_pid in "${digest_jobs[@]}"; do wait "$digest_pid" 2>/dev/null || true; done
}
trap 'stop_digest_jobs; exit 0' TERM INT
trap stop_digest_jobs EXIT

inbox_unread_file() {
  local owner=$1 key
  key=$(printf '%s' "$owner" | sha256sum | awk '{print $1}')
  printf '%s/inbox_unread/%s.tsv\n' "$watch_dir" "$key"
}

record_inbox_append() {
  local owner=$1 inbox_size=$2 now_epoch=$3 route=$4 agent=$5 state
  state=$(inbox_unread_file "$owner")
  mkdir -p "$(dirname "$state")"
  printf '%s\t%s\t0\t%s\t%s\t%s\n' "$now_epoch" "$inbox_size" "$owner" "$route" "$agent" >> "$state"
}

realert_unread_inboxes() {
  local state now_epoch cursor line_epoch line_size line_alerted owner route agent entry pid key file active
  now_epoch=$(date +%s)
  for state in "$watch_dir"/inbox_unread/*.tsv; do
    [[ -f $state ]] || continue
    local tmp="$state.tmp.$$"
    : > "$tmp"
    while IFS=$'\t' read -r line_epoch line_size line_alerted owner route agent || [[ -n ${line_epoch:-} ]]; do
      [[ $line_epoch =~ ^[0-9]+$ && $line_size =~ ^[0-9]+$ && -n ${owner:-} ]] || continue
      route=${route:-*}
      agent=${agent:-unknown}
      cursor=0
      active=0
      if [[ $route == '*' ]]; then
        for entry in "$owner"/.claude/.inbox_wait.d/*; do
          [[ -f $entry ]] || continue
          pid=${entry##*/}
          is_inbox_wait_pid "$pid" || continue
          active=1
          key=$(sed -n '2p' "$entry")
          file="$owner/.claude/.inbox_cursor"
          [[ -z $key ]] || file="$owner/.claude/.inbox_cursor.d/$key"
          if [[ -r $file ]]; then
            key=$(<"$file")
            [[ $key =~ ^[0-9]+$ ]] && (( key > cursor )) && cursor=$key
          fi
        done
        # Waiters exit after output, so their durable cursors remain evidence
        # that a broadcast was delivered after their registrations disappear.
        for file in "$owner"/.claude/.inbox_cursor.d/*; do
          [[ -f $file && -r $file ]] || continue
          key=$(<"$file")
          [[ $key =~ ^[0-9]+$ ]] && (( key > cursor )) && cursor=$key
        done
        # Preserve the legacy shared cursor for old registrations and readers.
        file="$owner/.claude/.inbox_cursor"
        if [[ -r $file ]]; then
          key=$(<"$file")
          [[ $key =~ ^[0-9]+$ ]] && (( key > cursor )) && cursor=$key
        fi
      else
        file="$owner/.claude/.inbox_cursor.d/$route"
        [[ -r $file ]] && cursor=$(<"$file")
        [[ $cursor =~ ^[0-9]+$ ]] || cursor=0
        for entry in "$owner"/.claude/.inbox_wait.d/*; do
          [[ -f $entry ]] || continue
          pid=${entry##*/}
          is_inbox_wait_pid "$pid" || continue
          key=$(sed -n '2p' "$entry")
          [[ $key == "$route" ]] && active=1
        done
      fi
      if (( cursor < line_size && line_alerted != 1 )) && { (( active == 0 )) || (( now_epoch - line_epoch >= inbox_unread_sec )); }; then
        if command -v notify-send >/dev/null 2>&1; then
          if [[ $agent == codex ]]; then
            notify-send -u normal "team-harness: Codex 창 확인 필요" "$owner: 미확인 사건" 2>/dev/null || true
          else
            notify-send -u critical "team-harness: inbox 미수신" "$owner: 세션 ${route:0:8} waiter 없음 또는 미확인" 2>/dev/null || true
          fi
        fi
        if [[ $agent == codex ]]; then
          printf '%s [Codex 창 확인 필요] %s: cursor=%s < size=%s\n' "$(date '+%F %T')" "$owner" "$cursor" "$line_size" >> "$watch_dir/events.log"
        else
          printf '%s [inbox 미수신] %s: cursor=%s < size=%s\n' "$(date '+%F %T')" "$owner" "$cursor" "$line_size" >> "$watch_dir/events.log"
        fi
        line_alerted=1
      fi
      printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$line_epoch" "$line_size" "$line_alerted" "$owner" "$route" "$agent" >> "$tmp"
    done < "$state"
    mv "$tmp" "$state"
  done
}

handle_daemon_event() {
  local task=$1 event_type=$2 summary=$3 v_res=$4 owner=$5
  local name=${task#"$HOME"/}
  local now_time
  now_time=$(date '+%F %R')
  local now_sec
  now_sec=$(date '+%F %T')

  local digest_event
  case "$event_type" in
    완료) digest_event=done ;;
    실패|'실패 의심'|'워커 중단(인계 없음)') digest_event=failed ;;
    '워커 오류(쿼터·용량)') digest_event=quota ;;
    중단) digest_event=dead ;;
    멈춤|'긴 실행 대기') digest_event=stall ;;
    *) digest_event=unknown ;;
  esac
  queue_digest "$task" "$digest_event"

  local jev_suffix="" jev_advice=""
  if [[ $event_type == 실패 && ${TEAM_HARNESS_JEV:-} != 0 ]]; then
    jev_advice=$(th_jev -m team_harness.decisions.advisor failure --task "$task")
    [[ -z $jev_advice ]] || jev_suffix=" | $jev_advice"
  fi

  # 1. 소유 작업 공간의 .claude/inbox.md에 한 줄 append
  if [[ -n $owner && -d $owner ]]; then
    local inbox_file="$owner/.claude/inbox.md"
    local inbox_size now_epoch route inbox_offset
    route=$(cat "$task/.th_session" 2>/dev/null || true)
    [[ -n $route && $route != */* && $route != . && $route != .. && $route != '*' && $route != *$'\t'* && $route != *$'\n'* ]] || route='*'
    mkdir -p "$owner/.claude"
    cleanup_inbox_waiter_registry "$owner"
    touch "$inbox_file"
    (
      flock -x 202
      inbox_offset=$(wc -c < "$inbox_file")
      printf '%s\t%s\n' "$inbox_offset" "$route" >> "$owner/.claude/inbox.routes"
      printf '%s | %s | %s | %s | %s%s\n' "$now_time" "$name" "$event_type" "$summary" "$v_res" "$jev_suffix" >> "$inbox_file"
      inbox_size=$(wc -c < "$inbox_file")
      now_epoch=$(date +%s)
      record_inbox_append "$owner" "$inbox_size" "$now_epoch" "$route" "$(cat "$task/.th_session_agent" 2>/dev/null || printf unknown)"
    ) 202>"$owner/.claude/.inbox_cursor.lock"
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
  if [[ ${TH_JEV_WATCH:-on} != "off" && ${TEAM_HARNESS_JEV:-} != 0 ]]; then
    th_jev -m team_harness.decisions.watch_cli "$task" "$event_type" "$watch_dir"
  fi
}

while true; do
  # 자기 스크립트 또는 라이브러리 파일 변경 시 스스로 재실행 (exec)
  current_sig=$(get_script_sig)
  if [[ -n $current_sig && -n $last_sig && $current_sig != "$last_sig" ]]; then
    echo "th-watchd: script updated, restarting via exec..." >&2
    stop_digest_jobs
    exec "$self_path" "$@"
  fi

  if [[ -f $registry ]]; then
    while IFS= read -r task || [[ -n $task ]]; do
      [[ -n $task && -d $task ]] || continue
      inspect_task_event "$task" "$watch_dir" ".th_daemon" "$stall_sec" "" handle_daemon_event
    done < "$registry"
  fi

  realert_unread_inboxes

  if [[ ${TH_ONCE:-0} == 1 ]]; then
    # Test/one-shot mode owns its children and returns with no background jobs.
    for digest_pid in "${digest_jobs[@]}"; do wait "$digest_pid" || true; done
    break
  fi

  # Reap completed summary requests without retaining a growing PID list.
  active_jobs=()
  for digest_pid in "${digest_jobs[@]}"; do
    if kill -0 "$digest_pid" 2>/dev/null; then
      active_jobs+=("$digest_pid")
    else
      wait "$digest_pid" 2>/dev/null || true
    fi
  done
  digest_jobs=("${active_jobs[@]}")
  sleep "$poll_sec"
done

exit 0
