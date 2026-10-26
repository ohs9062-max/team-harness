#!/usr/bin/env bash
# th-inbox-wait: owner inbox의 새 사건만 출력해 코디네이터 백그라운드 명령을 깨운다.
# 사용: th-inbox-wait.sh [max-wait-sec]
set -u

script_path=$(readlink -f "${BASH_SOURCE[0]}") || {
  echo "[th-inbox-wait] 스크립트 경로를 해석할 수 없습니다." >&2
  exit 2
}
script_dir=$(cd "$(dirname "$script_path")" && pwd) || {
  echo "[th-inbox-wait] 스크립트 디렉터리를 해석할 수 없습니다." >&2
  exit 2
}
# shellcheck source=scripts/th-watch-lib.sh
if ! source "$script_dir/th-watch-lib.sh"; then
  echo "[th-inbox-wait] th-watch-lib.sh를 로드할 수 없습니다." >&2
  exit 2
fi

max_wait=${1:-21600}
poll_sec=${TH_POLL_SEC:-15}
case "$max_wait" in (*[!0-9]*|'') max_wait=21600 ;; esac
case "$poll_sec" in (*[!0-9]*|'') poll_sec=15 ;; esac
(( poll_sec < 1 )) && poll_sec=1

if ! owner=$(project_owner "$PWD") || [[ -z $owner ]]; then
  echo "[th-inbox-wait] inbox owner를 해석할 수 없습니다." >&2
  exit 2
fi
if ! owner=$(realpath -m "$owner") || [[ -z $owner || $owner == / ]]; then
  echo "[th-inbox-wait] 안전한 inbox owner를 해석할 수 없습니다." >&2
  exit 2
fi
inbox_dir="$owner/.claude"
inbox_file="$inbox_dir/inbox.md"
cursor_file="$inbox_dir/.inbox_cursor"
cursor_lock="$inbox_dir/.inbox_cursor.lock"
wait_registry="$inbox_dir/.inbox_wait.d"
wait_registration="$wait_registry/$$"
mkdir -p "$inbox_dir"
mkdir -p "$wait_registry"
touch "$inbox_file"

cleanup_pid() {
  rm -f "$wait_registration"
}
trap 'cleanup_pid; exit 0' TERM INT HUP
trap cleanup_pid EXIT

# Each waiter has a separate registration.  Cursor locking below still ensures
# that exactly one waiter prints and advances a given inbox event.
printf '%s\n' "$$" > "$wait_registration"

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
if check_inbox; then
  exit 0
fi
echo '[th-inbox-wait] 대기 시간 만료(사건 없음) — 다시 백그라운드로 실행할 것'
exit 0
