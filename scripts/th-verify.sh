#!/usr/bin/env bash
# Script-only verdicts; runnable without an activated virtual environment.
set -euo pipefail
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/../src/team_harness/worker_digest.py" verify "$@"
