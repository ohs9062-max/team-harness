#!/usr/bin/env bash
set -euo pipefail
# Canary children receive only the harness variables the canary sets itself.
while IFS= read -r name; do
  case "$name" in
    TH_*|TEAM_HARNESS_*|GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_COMMON_DIR|GIT_OBJECT_DIRECTORY|GIT_ALTERNATE_OBJECT_DIRECTORIES|GIT_PREFIX|GIT_QUARANTINE_PATH) unset "$name" ;;
  esac
done < <(compgen -e)
script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
exec python3 "$script_dir/th-canary.py" "$@"
