#!/usr/bin/env bash
# th-status: 요청받았을 때만 등록 작업을 한 번 요약 표로 출력한다.
set -u

script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)

source "$script_dir/th-watch-lib.sh"

usage() {
  cat <<'EOF'
사용법: th-status.sh [-a|--all] [-w|--watch [초]] [-h|--help]
  기본: 실행 중 및 최근 24시간 실패·검수실패만 짧게 표시
  -a, --all       전체 상태 표 표시
  -w, --watch [초] 기본 짧은 출력을 반복 갱신 (기본 10초)
EOF
}

all_mode=0
watch_seconds=""
while (($#)); do
  case $1 in
    -a|--all) all_mode=1 ;;
    -w|--watch)
      watch_seconds=10
      if [[ ${2:-} =~ ^[1-9][0-9]*$ ]]; then watch_seconds=$2; shift; fi
      ;;
    -h|--help) usage; exit 0 ;;
    *) : ;; # Preserve the former behavior: unrecognized arguments were ignored.
  esac
  shift
done

render() {
  th_python -m team_harness.status "$script_dir/../src/team_harness/worker_digest.py" "$all_mode" "${1:-0}"
}

if [[ -n $watch_seconds ]]; then
  while true; do
    clear
    render 1
    sleep "$watch_seconds"
  done
else
  render
fi
