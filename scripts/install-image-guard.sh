#!/usr/bin/env bash
# Install the TH-D33 Claude image guard without disturbing existing hooks.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_hook="$repo_dir/scripts/hooks/image_guard.py"
[[ -f $source_hook ]] || { echo "image guard source missing: $source_hook" >&2; exit 1; }

hooks_dir="$HOME/.claude/hooks"
settings="$HOME/.claude/settings.json"
backup="$HOME/.claude/settings.json.bak_20261015d"
mkdir -p "$hooks_dir/state"
install -m 755 "$source_hook" "$hooks_dir/image_guard.py"
if [[ -f $settings && ! -e $backup ]]; then
  cp -p "$settings" "$backup"
fi

python3 - "$settings" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() and path.read_text(encoding="utf-8").strip() else {}
hooks = data.setdefault("hooks", {})
command = "python3 ~/.claude/hooks/image_guard.py"

for entries in hooks.values():
    for entry in entries:
        entry["hooks"] = [
            hook for hook in entry.get("hooks", [])
            if "block_image_read.py" not in hook.get("command", "")
        ]
for event, entries in list(hooks.items()):
    hooks[event] = [entry for entry in entries if entry.get("hooks")]

def install(event: str, matcher: str | None = None) -> None:
    entries = hooks.setdefault(event, [])
    for entry in entries:
        if entry.get("matcher") == matcher:
            nested = entry.setdefault("hooks", [])
            if not any(hook.get("type") == "command" and hook.get("command") == command for hook in nested):
                nested.append({"type": "command", "command": command})
            return
    entry = {"hooks": [{"type": "command", "command": command}]}
    if matcher is not None:
        entry["matcher"] = matcher
    entries.append(entry)

install("UserPromptSubmit")
install("Stop")
install("StopFailure")
install("PreToolUse", "Read")
install("PreToolUse", "Bash")
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

printf 'installed image guard: %s (settings merged; backup: %s)\n' "$hooks_dir/image_guard.py" "$backup"
