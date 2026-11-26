#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
(( $# == 1 || $# == 3 )) || { echo 'usage: th-stop.sh TASK [--reason REASON]' >&2; exit 2; }
task=$(realpath -e -- "$1"); shift
reason='사용자 요청'
if (( $# )); then [[ $1 == --reason ]] || exit 2; reason=$2; fi
exec {task_lock}>"$task/.th_launch.lock"
flock -x "$task_lock"
"$script_dir/th-env.sh" -m team_harness.task_stop "$task" "$reason"
