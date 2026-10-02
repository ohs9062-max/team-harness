#!/usr/bin/env bash
# Install the coordinator journal rule in global instruction files, idempotently.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
begin='<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:BEGIN -->'
rule_template="$repo_dir/templates/journal_rule_block.md"
[[ -f $rule_template ]] || { echo "journal rule template missing: $rule_template" >&2; exit 1; }

insert_rule() {
  local target=$1 dir tmp
  dir=$(dirname "$target")
  mkdir -p "$dir"
  [[ -e $target ]] || : > "$target"
  if grep -qF "$begin" "$target"; then
    return
  fi
  tmp=$(mktemp "$dir/.journal.XXXXXX")
  # Keep this file-based insertion: the Markdown backticks in the rule must
  # never be interpreted as shell command substitutions.
  { cat "$rule_template"; printf '\n\n'; cat "$target"; } > "$tmp"
  mv "$tmp" "$target"
}

insert_rule "$HOME/.claude/CLAUDE.md"
insert_rule "$HOME/.codex/AGENTS.md"

# agy has no documented universal instruction-file location.  Support known
# local conventions without guessing a new one; report absence for the caller.
agy_target=""
for candidate in "$HOME/.agy/AGENTS.md" "$HOME/.agy/INSTRUCTIONS.md" "$HOME/.config/agy/AGENTS.md"; do
  if [[ -f $candidate ]]; then agy_target=$candidate; break; fi
done
if [[ -n $agy_target ]]; then
  insert_rule "$agy_target"
  printf 'agy: installed %s\n' "$agy_target"
else
  printf 'agy: global instruction file not found; not installed\n' >&2
fi

# Preserve settings.json and retain a dated backup before a JSON merge.
settings="$HOME/.claude/settings.json"
mkdir -p "$(dirname "$settings")"
[[ -f $settings ]] && cp -p "$settings" "$settings.bak.$(date +%Y%m%d%H%M%S)"
python3 - "$settings" "$repo_dir/scripts/journal-hook.sh" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
hook = sys.argv[2]
data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
hooks = data.setdefault("hooks", {})
entries = hooks.setdefault("SessionStart", [])
command = f'"{hook}"'
for entry in entries:
    if entry.get("matcher") == "startup|resume|compact":
        nested = entry.setdefault("hooks", [])
        if not any(h.get("type") == "command" and h.get("command") == command for h in nested):
            nested.append({"type": "command", "command": command})
        break
else:
    entries.append({"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": command}]})
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY
printf 'claude/codex: installed; Claude SessionStart hook merged\n'
