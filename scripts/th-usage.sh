#!/usr/bin/env bash
# th-usage: standard-library ledger renderer; safe from an installed symlink.
set -euo pipefail
script_dir=$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)
exec python3 "$script_dir/../src/team_harness/usage_ledger.py" "$@"
