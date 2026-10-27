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

# Claude Code wins over inherited Codex environment markers.
if [[ -z ${CLAUDECODE:-} ]]; then
  codex_session=0
  while IFS='=' read -r name _; do
    [[ $name == CODEX_* ]] && codex_session=1
  done < <(env)
  parent=$PPID
  while [[ $parent =~ ^[0-9]+$ && $parent -gt 1 && -r /proc/$parent/stat ]]; do
    comm=$(cat "/proc/$parent/comm" 2>/dev/null || true)
    [[ $comm == codex || $comm == codex-* ]] && codex_session=1
    parent=$(awk '{print $4}' "/proc/$parent/stat" 2>/dev/null || printf 0)
  done
  if (( codex_session )); then
    echo '[th-inbox-wait] Codex 코디네이터는 waiter를 쓰지 않는다 — 사용자가 물을 때 th-inbox-peek로 확인'
    exit 3
  fi
fi
max_wait=${1:-21600}
poll_sec=${TH_POLL_SEC:-15}
case "$max_wait" in (*[!0-9]*|'') max_wait=21600 ;; esac
case "$poll_sec" in (*[!0-9]*|'') poll_sec=15 ;; esac
(( poll_sec < 1 )) && poll_sec=1
if ! owner=$(project_owner "$PWD") || [[ -z $owner ]]; then
  echo '[th-inbox-wait] inbox owner를 해석할 수 없습니다.' >&2
  exit 2
fi
if ! owner=$(realpath -m "$owner") || [[ -z $owner || $owner == / ]]; then
  echo '[th-inbox-wait] 안전한 inbox owner를 해석할 수 없습니다.' >&2
  exit 2
fi
session=$(th_session_key)
if [[ $session == */* || $session == . || $session == .. || $session == '*' || $session == *$'\n'* || $session == *$'\t'* ]]; then
  echo '[th-inbox-wait] 잘못된 세션 키' >&2
  exit 2
fi
inbox_dir="$owner/.claude"
wait_registry="$inbox_dir/.inbox_wait.d"
wait_registration="$wait_registry/$$"
mkdir -p "$wait_registry"
touch "$inbox_dir/inbox.md"
trap 'rm -f "$wait_registration"' EXIT
trap 'exit 0' TERM INT HUP
printf '%s\n%s\n' "$$" "$session" > "$wait_registration"
check_inbox() {
  python3 "$script_dir/th-inbox-read.py" "$owner" 1 wait
}
deadline=$(( $(date +%s) + max_wait ))
while (( $(date +%s) < deadline )); do
  check_inbox && exit 0
  sleep "$poll_sec"
done
check_inbox && exit 0
echo '[th-inbox-wait] 대기 시간 만료(사건 없음) — 다시 백그라운드로 실행할 것'
