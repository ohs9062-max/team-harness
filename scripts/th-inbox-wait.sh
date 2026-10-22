#!/usr/bin/env bash
# th-inbox-wait: owner inbox의 새 사건만 출력해 코디네이터 백그라운드 명령을 깨운다.
# 사용: th-inbox-wait.sh [max-wait-sec]
set -u

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/th-watch-lib.sh
source "$script_dir/th-watch-lib.sh"

max_wait=${1:-21600}
poll_sec=${TH_POLL_SEC:-15}
case "$max_wait" in (*[!0-9]*|'') max_wait=21600 ;; esac
case "$poll_sec" in (*[!0-9]*|'') poll_sec=15 ;; esac
(( poll_sec < 1 )) && poll_sec=1

owner=$(project_owner "$PWD")
owner=$(realpath -m "$owner")
inbox_dir="$owner/.claude"
inbox_file="$inbox_dir/inbox.md"
cursor_file="$inbox_dir/.inbox_cursor"
cursor_lock="$inbox_dir/.inbox_cursor.lock"
pid_file="$inbox_dir/.inbox_wait.pid"
pid_lock="$inbox_dir/.inbox_wait.pid.lock"
mkdir -p "$inbox_dir"
touch "$inbox_file"

is_inbox_wait_pid() {
  local pid=$1 cmdline
  [[ $pid =~ ^[1-9][0-9]*$ && -r /proc/$pid/cmdline ]] || return 1
  cmdline=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
  [[ $cmdline == *th-inbox-wait.sh* ]] && kill -0 "$pid" 2>/dev/null
}

cleanup_pid() {
  local recorded
  recorded=""
  [[ -r $pid_file ]] && recorded=$(<"$pid_file")
  [[ $recorded == "$$" ]] && rm -f "$pid_file"
}
trap 'cleanup_pid; exit 0' TERM INT HUP
trap cleanup_pid EXIT

# A later waiter supersedes an earlier one for this owner.  Check its cmdline
# before signalling so an unrelated recycled PID is never killed.
(
  flock -x 201
  previous=""
  [[ -r $pid_file ]] && previous=$(<"$pid_file")
  if is_inbox_wait_pid "$previous"; then
    kill -TERM "$previous" 2>/dev/null || true
  fi
  printf '%s\n' "$$" > "$pid_file"
) 201>"$pid_lock"

check_inbox() {
  (
    flock -x 201
    local size cursor=0 start=0 complete_bytes=0 line
    local -a lines=()
    size=$(wc -c < "$inbox_file")
    if [[ -f $cursor_file ]]; then
      cursor=$(<"$cursor_file")
      [[ $cursor =~ ^[0-9]+$ ]] || cursor=$size
    else
      # First use starts at EOF: do not wake a newly started coordinator for
      # history it has already had a chance to process.
      printf '%s\n' "$size" > "$cursor_file"
      exit 1
    fi
    (( size < cursor )) && cursor=0
    start=$cursor

    while IFS= read -r line; do
      lines+=("$line")
      complete_bytes=$(( complete_bytes + $(printf '%s\n' "$line" | wc -c) ))
    done < <(tail -c "+$(( start + 1 ))" "$inbox_file")

    (( ${#lines[@]} > 0 )) || exit 1
    printf '[th-inbox-wait] 새 사건 %d건 — 처리 후 이 명령을 다시 백그라운드로 실행할 것\n' "${#lines[@]}"
    printf '%s\n' "${lines[@]}"
    # Advance only after the complete lines have reached stdout.
    printf '%s\n' "$(( start + complete_bytes ))" > "$cursor_file"
  ) 201>"$cursor_lock"
}

deadline=$(( $(date +%s) + max_wait ))
while (( $(date +%s) < deadline )); do
  check_inbox && exit 0
  sleep "$poll_sec"
done

# Also inspect once for max-wait=0 and for an event arriving at the deadline.
check_inbox || true
exit 0
