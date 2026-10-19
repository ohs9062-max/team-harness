#!/usr/bin/env bash
# Claude Code SessionStart hook: make a workspace status file available as context.
set -euo pipefail

[[ ${TH_ROLE:-} == worker ]] && exit 0

workspace=${CLAUDE_PROJECT_DIR:-$PWD}
status="$workspace/.claude/current_status.md"
[[ -f $status ]] && cat "$status"
