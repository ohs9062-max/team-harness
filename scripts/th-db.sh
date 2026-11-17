#!/usr/bin/env bash
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
exec "$script_dir/th-env.sh" -m team_harness.state_index "$@"
