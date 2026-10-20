#!/usr/bin/env bash
# Claude Code SessionStart hook: make a workspace status file available as context.
set -euo pipefail

[[ ${TH_ROLE:-} == worker ]] && exit 0

workspace=${CLAUDE_PROJECT_DIR:-$PWD}
status="$workspace/.claude/current_status.md"
if [[ -f $status ]]; then
  cat "$status"
fi

script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/th-watch-lib.sh
source "$script_dir/th-watch-lib.sh"
owner=$(project_owner "$workspace")
inbox="$owner/.claude/inbox.md"

if [[ ${1:-} == prompt ]]; then
  offset_file="$workspace/.claude/.inbox_offset"
  mkdir -p "$(dirname "$offset_file")"
  current_size=0
  [[ -f $inbox ]] && current_size=$(wc -c < "$inbox")
  if [[ ! -f $offset_file ]]; then
    printf '%s\n' "$current_size" > "$offset_file"
    exit 0
  fi
  previous_size=$(<"$offset_file")
  [[ $previous_size =~ ^[0-9]+$ ]] || previous_size=0
  if (( current_size > previous_size )); then
    printf '## 새 사건\n'
    tail -c "+$(( previous_size + 1 ))" "$inbox"
  fi
  printf '%s\n' "$current_size" > "$offset_file"
elif (( $# == 0 )) && [[ -f $inbox ]]; then
  printf '## inbox (최근)\n'
  tail -n 10 "$inbox"
fi
