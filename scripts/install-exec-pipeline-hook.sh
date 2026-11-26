#!/usr/bin/env bash
set -euo pipefail
repo_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
hooks_dir=${TH_CLAUDE_HOOKS_DIR:-$HOME/.claude/hooks}
settings=${TH_CLAUDE_SETTINGS:-$HOME/.claude/settings.json}
mkdir -p "$hooks_dir" "$(dirname "$settings")"
install -m 755 "$repo_dir/scripts/hooks/th-exec-pipeline.py" "$hooks_dir/th-exec-pipeline.py"
python3 - "$settings" "$repo_dir" <<'PY'
import json
from pathlib import Path
import sys
path, root = map(Path, sys.argv[1:])
sys.path.insert(0, str(root / "src/team_harness"))
from hook_identity import deployed_paths, managed_hook
data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
hooks = data.setdefault('hooks', {})
command = f'TH_HARNESS_SRC={str(root / "src")!r} python3 ~/.claude/hooks/th-exec-pipeline.py'
for event in ('PreToolUse', 'PostToolUse'):
    entries = hooks.setdefault(event, [])
    for entry in entries:
        entry['hooks'] = [h for h in entry.get('hooks', []) if not managed_hook(h, deployed_paths('th-exec-pipeline.py'), allowed_env={'TH_HARNESS_SRC': str(root / 'src')})]
    entries[:] = [entry for entry in entries if entry.get('hooks')]
    target = next((entry for entry in entries if entry.get('matcher') == '^Bash$'), None)
    if target is None:
        target = {'matcher': '^Bash$', 'hooks': []}
        entries.append(target)
    target['hooks'].append({'type': 'command', 'command': command})
path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
PY
