#!/usr/bin/env bash
set -euo pipefail
# Canary children receive only the harness variables the canary sets itself.
while IFS= read -r name; do
  case "$name" in
    TH_*|TEAM_HARNESS_*) unset "$name" ;;
  esac
done < <(compgen -e)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/th-canary.py" "$@"
