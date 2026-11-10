#!/usr/bin/env bash
# Script-only verdicts; runnable without an activated virtual environment.
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
if [[ ${1:-} == --reclassify ]]; then
  shift
  export PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}"
  exec python3 -m team_harness.verify_reclassify "$@"
fi
exec python3 "$script_dir/../src/team_harness/worker_digest.py" verify "$@"
