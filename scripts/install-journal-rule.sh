#!/usr/bin/env bash
# Install the coordinator journal rule in global instruction files, idempotently.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
begin='<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:BEGIN -->'
end='<!-- TEAM-HARNESS-COORDINATOR-JOURNAL:END -->'
rule_template="$repo_dir/templates/journal_rule_block.md"
[[ -f $rule_template ]] || { echo "journal rule template missing: $rule_template" >&2; exit 1; }

install_rule() {
  local target=$1 dir tmp
  dir=$(dirname "$target")
  mkdir -p "$dir"
  [[ -e $target ]] || : > "$target"
  tmp=$(mktemp "$dir/.journal.XXXXXX")
  # Replace the marked block if present, retaining all content outside it.
  # Keep this file-based operation: Markdown backticks must never be evaluated
  # by the shell.
  python3 - "$target" "$rule_template" "$tmp" "$begin" "$end" <<'PY'
from pathlib import Path
import sys

target = Path(sys.argv[1])
template = Path(sys.argv[2])
output = Path(sys.argv[3])
begin, end = sys.argv[4:6]
current = target.read_text(encoding="utf-8")
rule = template.read_text(encoding="utf-8").rstrip()
first = current.find(begin)
if first < 0:
    rewritten = f"{rule}\n\n{current}"
else:
    last = current.find(end, first)
    if last < 0:
        raise SystemExit(f"unterminated journal rule block in {target}")
    last += len(end)
    if current.find(begin, last) >= 0:
        raise SystemExit(f"multiple journal rule blocks in {target}")
    rewritten = current[:first] + rule + current[last:]
output.write_text(rewritten, encoding="utf-8")
PY
  mv "$tmp" "$target"
}

install_rule "$HOME/.claude/CLAUDE.md"
install_rule "$HOME/.codex/AGENTS.md"

# agy has no documented universal instruction-file location.  Support known
# local conventions without guessing a new one; report absence for the caller.
agy_target=""
for candidate in "$HOME/.agy/AGENTS.md" "$HOME/.agy/INSTRUCTIONS.md" "$HOME/.config/agy/AGENTS.md"; do
  if [[ -f $candidate ]]; then agy_target=$candidate; break; fi
done
if [[ -n $agy_target ]]; then
  install_rule "$agy_target"
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
command = f'"{hook}"'
prompt_command = f'"{hook}" prompt'

def is_journal_hook(item):
    return item.get("type") == "command" and "journal-hook.sh" in item.get("command", "")

session_entries = hooks.setdefault("SessionStart", [])
kept_session = []
installed = False
for entry in session_entries:
    nested = [item for item in entry.get("hooks", []) if not is_journal_hook(item)]
    if entry.get("matcher") == "startup|resume|compact" and not installed:
        nested.append({"type": "command", "command": command})
        installed = True
    if nested:
        entry["hooks"] = nested
        kept_session.append(entry)
if not installed:
    kept_session.append({"matcher": "startup|resume|compact", "hooks": [{"type": "command", "command": command}]})
hooks["SessionStart"] = kept_session

prompt_entries = hooks.setdefault("UserPromptSubmit", [])
kept_prompt = []
prompt_installed = False
for entry in prompt_entries:
    nested = [item for item in entry.get("hooks", []) if not is_journal_hook(item)]
    if not prompt_installed:
        nested.append({"type": "command", "command": prompt_command})
        prompt_installed = True
    if nested:
        entry["hooks"] = nested
        kept_prompt.append(entry)
if not prompt_installed:
    kept_prompt.append({"hooks": [{"type": "command", "command": prompt_command}]})
hooks["UserPromptSubmit"] = kept_prompt
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
PY
printf 'claude/codex: installed; Claude SessionStart hook merged\n'
