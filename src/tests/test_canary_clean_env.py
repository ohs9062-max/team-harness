"""The canary entry point must not inherit worker or watcher settings."""

import os
from pathlib import Path
import subprocess


def test_canary_entry_clears_worker_environment(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    python = tmp_path / "python3"
    python.write_text('#!/bin/sh\nenv > "$CANARY_ENV_CAPTURE"\n')
    python.chmod(0o755)
    capture = tmp_path / "environment"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "CANARY_ENV_CAPTURE": str(capture),
        "TH_WATCH_ROLLOVER": "1",
        "TH_ROLE": "worker",
        "TH_WATCH_DIR": "/inherited/watch",
        "TEAM_HARNESS_CANARY_TEST": "1",
    }
    subprocess.run([str(root / "scripts/th-canary.sh"), str(root)], env=env, check=True)
    captured = dict(line.split("=", 1) for line in capture.read_text().splitlines())
    assert not any(k.startswith(("TH_", "TEAM_HARNESS_")) for k in captured)
    assert captured["HOME"] == env["HOME"]
    assert captured["PATH"] == env["PATH"]
