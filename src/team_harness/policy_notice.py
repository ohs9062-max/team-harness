"""Deliver deployed policy changes once per coordinator session."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

MAX_SESSIONS = 128


def notice(workspace: Path, payload: dict, *, session_start: bool = False) -> str:
    if os.environ.get("TH_ROLE") == "worker":
        return ""
    # Startup already loads global instructions and the current policy. Resume
    # and compaction must preserve the old version until the next user prompt.
    if session_start and payload.get("source") not in {"startup", "clear"}:
        return ""
    directory = Path.home() / ".team-harness"
    try:
        deployment = json.loads((directory / "deployment.json").read_text())
        version = deployment["files"][str(directory / "operating_policy.md")]
        session = str(
            payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or ""
        )
        if not session:
            return ""  # Never share a cursor between unknown sessions.
        key = hashlib.sha256(session.encode()).hexdigest()[:20]
        state_dir = workspace / ".claude"
        state_dir.mkdir(parents=True, exist_ok=True)
        state = state_dir / ".policy_versions.json"
        legacy = state_dir / f".policy_version.{key}"
        with (state_dir / ".policy_versions.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                cursors = json.loads(state.read_text())
                if not isinstance(cursors, dict):
                    cursors = {}
            except (OSError, ValueError):
                cursors = {}
            previous = cursors.get(key)
            if previous is None and legacy.is_file():
                previous = legacy.read_text()
            if previous == version and not session_start:
                return ""
            policy = (directory / "operating_policy.md").read_text(encoding="utf-8")
            if hashlib.sha256(policy.encode()).hexdigest() != version:
                return "team-harness 정책 파일과 배포 해시가 다릅니다. th-sync.sh --status를 확인하세요."
            cursors.pop(key, None)
            cursors[key] = version
            cursors = dict(list(cursors.items())[-MAX_SESSIONS:])
            temporary = state.with_suffix(".tmp")
            temporary.write_text(json.dumps(cursors) + "\n")
            temporary.chmod(0o600)
            temporary.replace(state)
            legacy.unlink(missing_ok=True)
        if session_start:
            return ""
        return f"## team-harness 정책 업데이트 {deployment['revision'][:12]}\n{policy}"
    except (OSError, ValueError, KeyError, TypeError):
        return ""


if __name__ == "__main__":
    try:
        request = json.load(sys.stdin)
    except (OSError, ValueError):
        request = {}
    print(
        notice(
            Path(sys.argv[1]),
            request if isinstance(request, dict) else {},
            session_start="--session-start" in sys.argv[2:],
        ),
        end="",
    )
