"""Resolve coordinator models only from transcripts bound to a process session."""

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

_UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
_TAIL = 256 * 1024


def session_for(vendor: Any, args: Any, env: Any, pid: Any = None, start: Any = None):
    if vendor.startswith("codex"):
        return env.get(b"CODEX_THREAD_ID", b"").decode(errors="ignore") or env.get(
            b"CODEX_SESSION_ID", b""
        ).decode(errors="ignore")
    if vendor == "claude":
        if pid is not None and start is not None:
            try:
                data = json.loads(
                    (Path.home() / ".claude/sessions" / f"{pid}.json").read_text()
                )
                sid = data.get("sessionId")
                if (
                    str(data.get("procStart")) == str(start)
                    and isinstance(sid, str)
                    and _UUID.fullmatch(sid)
                ):
                    return sid
            except (OSError, ValueError, TypeError, AttributeError):
                pass
        for i, arg in enumerate(args):
            if arg in ("--resume", "-r", "--session-id") and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(("--resume=", "--session-id=")):
                return arg.split("=", 1)[1]
        return env.get(b"CLAUDE_CODE_SESSION_ID", b"").decode(errors="ignore")
    return ""


def _model(event: Any, vendor: Any, session: Any):
    if not isinstance(event, dict):
        return ""
    if vendor.startswith("codex"):
        payload = event.get("payload")
        if event.get("type") == "turn_context" and isinstance(payload, dict):
            value = payload.get("model")
        else:
            return ""
    else:
        if event.get("type") != "assistant" or event.get("sessionId") != session:
            return ""
        message = event.get("message")
        value = message.get("model") if isinstance(message, dict) else None
    return (
        value if isinstance(value, str) and value and not value.startswith("<") else ""
    )


def resolve_model(
    vendor: Any,
    session: Any,
    pid: Any,
    start: Any,
    home: Any = None,
    cache_dir: Any = None,
    persist_cache: Any = True,
):
    if not _UUID.fullmatch(session):
        return ""
    home = Path(home or Path.home())
    root = home / (
        ".codex/sessions" if vendor.startswith("codex") else ".claude/projects"
    )
    pattern = (
        f"rollout-*-{session}.jsonl"
        if vendor.startswith("codex")
        else f"{session}.jsonl"
    )
    try:
        matches = list(root.rglob(pattern))
        if len(matches) != 1:
            return ""
        path = matches[0]
        stat = path.stat()
        if not persist_cache:
            model = ""
            with path.open("rb") as stream:
                offset = max(0, stat.st_size - _TAIL)
                stream.seek(offset)
                if offset:
                    stream.readline()
                for line in stream:
                    try:
                        model = _model(json.loads(line), vendor, session) or model
                    except (ValueError, UnicodeDecodeError):
                        continue
            return model
        cache_root = Path(
            cache_dir
            or (Path(tempfile.gettempdir()) / f"th-coordinator-model-{os.getuid()}")
        )
        cache_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        cache_path = cache_root / f"{pid}-{start}-{session}.json"
        try:
            cached = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            cached = {}
        identity = [str(path), stat.st_dev, stat.st_ino]
        same = cached.get("identity") == identity
        if (
            same
            and cached.get("mtime") == stat.st_mtime_ns
            and cached.get("size") == stat.st_size
        ):
            return cached.get("model", "")
        offset = (
            cached.get("size", 0)
            if same and stat.st_size >= cached.get("size", 0)
            else 0
        )
        model = cached.get("model", "") if offset else ""
        offset = max(offset, stat.st_size - _TAIL)
        with path.open("rb") as stream:
            stream.seek(offset)
            if offset and (not same or offset != cached.get("size")):
                stream.readline()
            for line in stream:
                try:
                    model = _model(json.loads(line), vendor, session) or model
                except (ValueError, UnicodeDecodeError):
                    continue
        data = {
            "identity": identity,
            "mtime": stat.st_mtime_ns,
            "size": stat.st_size,
            "model": model,
        }
        fd, tmp = tempfile.mkstemp(dir=cache_root)
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream)
        os.replace(tmp, cache_path)
        return model
    except (OSError, ValueError, TypeError):
        return ""
