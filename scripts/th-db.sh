#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
export PYTHONPATH="$script_dir/../src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m team_harness.state_index "$@"
