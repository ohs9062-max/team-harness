#!/usr/bin/env bash
# Isolate pytest before plugin discovery and process observation start.
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
test_home=$(mktemp -d "${TMPDIR:-/tmp}/th-test-home.XXXXXXXX")
trap 'rm -rf -- "$test_home"' EXIT
export HOME="$test_home" TH_WATCH_DIR="$test_home/watch"
export PYTEST_DISABLE_PLUGIN_AUTOLOAD=1
unset TH_INBOX_OWNER TH_WORKSPACE CLAUDE_PROJECT_DIR CLAUDE_CODE_SESSION_ID CLAUDE_PID
unset TH_ROLE TH_SESSION_KEY TH_JOB_TASK TH_WORKER_OUTPUT_DIR TH_WORKER_ALLOW_PATHS ORCA_PANE_KEY
cd "$root"
uv run --extra dev "$root/scripts/th-env.sh" -m pytest "$@"
