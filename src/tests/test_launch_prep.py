from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


def invoke(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "team_harness.launch_prep", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def test_usage_preserves_shell_ledger_format() -> None:
    proc = invoke(
        "usage",
        "2026-11-17T00:00:00Z",
        'task "한글"',
        "2",
        "codex",
        "model",
        "high",
        "/owner",
        "",
        "false",
    )
    assert proc.returncode == 0
    assert proc.stdout == (
        '{"v":1,"at":"2026-11-17T00:00:00Z","task":'
        + json.dumps('task "한글"')
        + ',"attempt":2,"worker":"codex","model":"model","effort":"high",'
        '"owner":"/owner","override":null,"rollover":false}\n'
    )


def test_context_readers(tmp_path: Path) -> None:
    path = tmp_path / "ctx.json"
    path.write_text(
        json.dumps(
            {
                "enabled": True,
                "mode": "resume",
                "session_id": "abc",
                "preserve_job": True,
                "handoff_text": "a\nb",
                "allow_paths": ["/한글"],
            }
        )
    )
    assert invoke("meta", str(path)).stdout == "1\nresume\nabc\n1\n"
    assert invoke("handoff", str(path)).stdout == "a\nb"
    assert invoke("allow-paths", str(path)).stdout == json.dumps(["/한글"]) + "\n"
