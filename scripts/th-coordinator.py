#!/usr/bin/env python3
"""Capture a launcher coordinator using the shared identity implementation."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from team_harness.coordinator_identity import alive, capture  # noqa: F401, E402

if __name__ == "__main__":
    data = capture(int(sys.argv[1]), sys.argv[2])
    Path(sys.argv[3]).write_text(json.dumps(data, ensure_ascii=False) + "\n")
