#!/usr/bin/env bash
set -euo pipefail
if (( $# < 3 )); then echo 'usage: th-job-start.sh TASK [--log FILE] -- COMMAND...' >&2; exit 2; fi
task=$(realpath -e -- "$1"); shift
[[ -d $task ]] || exit 2
exec {task_lock}>"$task/.th_launch.lock"
flock -x "$task_lock"
[[ ! -e $task/STOPPED_BY_USER ]] || { echo "사용자가 정지한 작업입니다" >&2; exit 1; }
if [[ -n ${TH_JOB_START_NOTIFY_FD:-} ]]; then
  printf R >&"$TH_JOB_START_NOTIFY_FD"
fi
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
wait_mode=0
if [[ ${1:-} == --wait ]]; then wait_mode=1; shift; fi
log="$task/logs/job.log"
if [[ ${1:-} == --log ]]; then [[ $# -ge 3 ]] || exit 2; log=$2; shift 2; fi
[[ ${1:-} == -- && $# -ge 2 ]] || exit 2
shift
mkdir -p -- "$(dirname "$log")"
rm -f -- "$task/job.pid" "$task/job.pid.start"
export TH_JOB_TASK="$task" TH_JOB_PY="$script_dir/th-env.sh"
if (( wait_mode )); then
  exec {input_fd}<&0
  if [[ -t 1 ]]; then
    printf '[th] stdout is a terminal; not captured\n' >> "$log"
    exec {output_fd}>&1
  else
    exec {output_fd}> >(exec {task_lock}>&-; tee -a "$log")
  fi
  if [[ -t 2 ]]; then
    printf '[th] stderr is a terminal; not captured\n' >> "$log"
    exec {error_fd}>&2
  else
    exec {error_fd}> >(exec {task_lock}>&-; tee -a "$log" >&2)
  fi
  setsid bash -c '"$TH_JOB_PY" -c "import json,sys; from team_harness.agents.process_identity import capture_starttime; from pathlib import Path; p=Path(sys.argv[1]); pid=int(sys.argv[2]); start=capture_starttime(pid); assert start; (p / \"job.pid.start\").write_text(json.dumps({\"pid\":pid,\"start\":start}))" "$TH_JOB_TASK" "$$" && unset TH_JOB_PY && echo $$ > "$TH_JOB_TASK/job.pid.tmp" && mv -f -- "$TH_JOB_TASK/job.pid.tmp" "$TH_JOB_TASK/job.pid" && { if [[ -n ${TH_JOB_START_NOTIFY_FD:-} ]]; then printf S >&"$TH_JOB_START_NOTIFY_FD" && exec {TH_JOB_START_NOTIFY_FD}>&- && unset TH_JOB_START_NOTIFY_FD; fi; } && exec "$@"' _ "$@" <&$input_fd >&$output_fd 2>&$error_fd {output_fd}>&- {error_fd}>&- {task_lock}>&- &
  child=$!
  exec {input_fd}<&-
  exec {output_fd}>&- {error_fd}>&-
  trap 'kill -INT -- "-$child" 2>/dev/null || : ' INT
  trap 'kill -TERM -- "-$child" 2>/dev/null || : ' TERM
  for ((i=0; i<100; i++)); do
    [[ -s $task/job.pid ]] && break
    sleep 0.05
  done
  flock -u "$task_lock"
  exec {task_lock}>&-
  set +e
  wait "$child"
  rc=$?
  if kill -0 "$child" 2>/dev/null; then wait "$child"; rc=$?; fi
  set -e
  exit "$rc"
fi
setsid bash -c '"$TH_JOB_PY" -c "import json,sys; from team_harness.agents.process_identity import capture_starttime; from pathlib import Path; p=Path(sys.argv[1]); pid=int(sys.argv[2]); start=capture_starttime(pid); assert start; (p / \"job.pid.start\").write_text(json.dumps({\"pid\":pid,\"start\":start}))" "$TH_JOB_TASK" "$$" && unset TH_JOB_PY && echo $$ > "$TH_JOB_TASK/job.pid.tmp" && mv -f -- "$TH_JOB_TASK/job.pid.tmp" "$TH_JOB_TASK/job.pid" && exec "$@"' _ "$@" </dev/null >>"$log" 2>&1 {task_lock}>&- &
for ((i=0; i<100; i++)); do
  if [[ -s $task/job.pid ]]; then
    cat "$task/job.pid"; exit 0
  fi
  sleep 0.05
done
echo 'job PID was not recorded within 5 seconds' >&2
exit 1
