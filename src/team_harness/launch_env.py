"""Persist only launch policy overrides for replacement workers."""

import json
import os
from pathlib import Path
import shlex
import sys

KEYS = frozenset(
    {
        "TH_HIGH_REASON",
        "TH_CODEX_REASON",
        "TH_MAX_PARALLEL",
        "TH_MAX_PARALLEL_CODEX",
        "TH_MAX_PARALLEL_AGY",
        "TH_MAX_PARALLEL_CLAUDE",
        "TH_IGNORE_BUDGET",
        "TH_BRIEF_OK",
        "TH_USER_REQUESTED_CLAUDE",
        "TH_IGNORE_QUOTA_LOCK",
        "TH_MAX_RETRIES",
        "TH_CODEX_5H_BUDGET",
    }
)


def main() -> None:
    action, task = sys.argv[1:]
    path = Path(task) / ".th_launch_env"
    if action == "save":
        values = {key: os.environ[key] for key in KEYS if key in os.environ}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream, ensure_ascii=False)
    elif path.is_file():
        values = json.loads(path.read_text(encoding="utf-8"))
        for key in sorted(KEYS):
            print(f"unset {key}")
        for key, value in values.items():
            if key in KEYS and isinstance(value, str):
                print(f"export {key}={shlex.quote(value)}")


if __name__ == "__main__":
    main()
