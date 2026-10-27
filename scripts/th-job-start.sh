#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then echo 'usage: th-job-start.sh TASK [--log FILE] -- COMMAND...' >&2; exit 2; fi
task=$(realpath -e -- "$1"); shift
[[ -d $task ]] || exit 2
log="$task/logs/job.log"
if [[ ${1:-} == --log ]]; then [[ $# -ge 3 ]] || exit 2; log=$2; shift 2; fi
[[ ${1:-} == -- && $# -ge 2 ]] || exit 2
shift
mkdir -p -- "$(dirname "$log")"
rm -f -- "$task/job.pid"
export TH_JOB_TASK="$task"
setsid bash -c 'echo $$ > "$TH_JOB_TASK/job.pid.tmp" && mv -f -- "$TH_JOB_TASK/job.pid.tmp" "$TH_JOB_TASK/job.pid" && exec "$@"' _ "$@" </dev/null >>"$log" 2>&1 &
for ((i=0; i<100; i++)); do
  if [[ -s $task/job.pid ]]; then cat "$task/job.pid"; exit 0; fi
  sleep 0.05
done
echo 'job PID was not recorded within 5 seconds' >&2
exit 1
