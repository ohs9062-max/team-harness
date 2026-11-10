"""Deliver a deployed policy once per coordinator session and policy version."""

import hashlib
import json
import os
from pathlib import Path
import sys


def notice(workspace: Path, payload: dict) -> str:
    if os.environ.get("TH_ROLE") == "worker":
        return ""
    directory = Path.home() / ".team-harness"
    try:
        deployment = json.loads((directory / "deployment.json").read_text())
        version = deployment["files"][str(directory / "operating_policy.md")]
        session = str(
            payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or ""
        )
        if not session:
            return ""  # Never share a consumption cursor between unknown sessions.
        key = hashlib.sha256(session.encode()).hexdigest()[:20]
        cursor = workspace / ".claude" / f".policy_version.{key}"
        if cursor.exists() and cursor.read_text() == version:
            return ""
        policy = (directory / "operating_policy.md").read_text(encoding="utf-8")
        if hashlib.sha256(policy.encode()).hexdigest() != version:
            return "team-harness 정책 파일과 배포 해시가 다릅니다. th-sync.sh --status를 확인하세요."
        cursor.parent.mkdir(parents=True, exist_ok=True)
        cursor.write_text(version)
        return f"## team-harness 정책 업데이트 {deployment['revision'][:12]}\n{policy}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
    except (OSError, ValueError):
        request = {}
    print(
        notice(Path(sys.argv[1]), request if isinstance(request, dict) else {}), end=""
    )
