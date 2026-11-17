#!/usr/bin/env bash
# Verify, then make one bounded read-only summary request.
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
exec "$script_dir/th-env.sh" "$script_dir/../src/team_harness/worker_digest.py" digest "$@"
