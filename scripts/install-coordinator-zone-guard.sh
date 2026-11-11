#!/usr/bin/env bash
# Install the worker-only coordinator-zone PreToolUse guard without replacing
# unrelated user hooks.
set -euo pipefail

repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
source_hook="$repo_dir/scripts/hooks/coordinator_zone_guard.py"
hooks_dir="${TH_CLAUDE_HOOKS_DIR:-$HOME/.claude/hooks}"
settings="${TH_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}"
codex_hooks="${TH_CODEX_HOOKS:-$HOME/.codex/hooks.json}"
codex_backup="${TH_CODEX_HOOKS_BACKUP:-$HOME/.codex/hooks.json.bak_20261019c}"
[[ -f $source_hook ]] || { echo "coordinator zone guard source missing" >&2; exit 1; }
mkdir -p "$hooks_dir" "$(dirname "$settings")" "$(dirname "$codex_hooks")"
if ! cmp -s "$source_hook" "$hooks_dir/coordinator_zone_guard.py"; then
  install -m 755 "$source_hook" "$hooks_dir/coordinator_zone_guard.py"
fi
if [[ -f $codex_hooks && ! -f $codex_backup ]]; then
  cp -p "$codex_hooks" "$codex_backup"
fi
python3 - "$settings" "$codex_hooks" "$repo_dir/src" << 'PY'
import json
import sys
from pathlib import Path

claude_settings, codex_hooks, source = map(Path, sys.argv[1:])
sys.path.insert(0, str(source / "team_harness"))
from hook_identity import deployed_paths, managed_hook

command = f'TH_HARNESS_SRC="{source}" python3 ~/.claude/hooks/coordinator_zone_guard.py'


def upsert_pre_tool_use(path: Path, *, matcher: str | None) -> None:
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    hooks = data.setdefault("hooks", {})
    entries = hooks.setdefault("PreToolUse", [])
    kept = []
    for entry in entries:
        nested = [
            hook for hook in entry.get("hooks", [])
            if not managed_hook(hook, deployed_paths("coordinator_zone_guard.py"), allowed_env={"TH_HARNESS_SRC": str(source)})
        ]
        if nested:
            entry["hooks"] = nested
            kept.append(entry)
    entry_obj: dict[str, object] = {
        "hooks": [{"type": "command", "command": command}]
    }
    if matcher is not None:
        entry_obj["matcher"] = matcher
    kept.append(entry_obj)
    hooks["PreToolUse"] = kept
    updated = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    if not path.exists() or path.read_text(encoding="utf-8") != updated:
        path.write_text(updated, encoding="utf-8")


upsert_pre_tool_use(claude_settings, matcher="Read|Write|Edit|Bash")
upsert_pre_tool_use(codex_hooks, matcher=None)
PY

