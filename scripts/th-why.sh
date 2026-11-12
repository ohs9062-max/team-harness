#!/usr/bin/env bash
set -u
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$script_dir/th-watch-lib.sh"
if [[ $# != 1 ]]; then printf '사용법: th-why.sh <작업폴더|PID>\n' >&2; exit 2; fi
PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}" th_python -m team_harness.state_recall "$1"
