#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
source "$script_dir/th-watch-lib.sh"
th_python -m team_harness.deployment "$@"
