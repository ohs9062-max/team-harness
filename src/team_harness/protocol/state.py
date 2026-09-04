"""Atomic protocol state persistence alongside team-harness run.json."""

from __future__ import annotations

from datetime import datetime
from datetime import UTC
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from team_harness.protocol.models import ProtocolState

_SENSITIVE_PATTERNS = [
    re.compile(r"(?i)\b(authorization\s*[:=]\s*['\"]?(?:bearer\s+)?)([^\s'\"]{6,})"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._~+/-]{8,})"),
    re.compile(
        r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"]?([^\s'\"]{6,})"
    ),
    re.compile(
        r"\b(sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}"
        r"|xox[baprs]-[A-Za-z0-9-]{10,})\b"
    ),
]
_SENSITIVE_KEY = re.compile(r"(?i)(api[_-]?key|secret|token|password|authorization)")


def mask_sensitive(text: str) -> str:
    """Mask sensitive values in *text*."""
    result = text or ""
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(r"\1[MASKED]", result)
        else:
            result = pattern.sub("[MASKED]", result)
    return result


def _mask_value(value: Any, key: str = "") -> Any:
    """Recursively mask sensitive values before JSON serialization."""
    if key and _SENSITIVE_KEY.search(key):
        return "[MASKED]"
    if isinstance(value, dict):
        return {k: _mask_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_value(item) for item in value]
    if isinstance(value, str):
        return mask_sensitive(value)
    return value


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* as JSON to *path* atomically via rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


class ProtocolStateManager:
    """Manages protocol state persistence alongside team-harness run artifacts.

    State is stored as ``protocol_state.json`` in the TH run directory.
    Events are appended to ``protocol_events.jsonl``.
    """

    def __init__(self, run_dir: str | Path) -> None:
        self.run_dir = Path(run_dir).resolve()

    @property
    def state_path(self) -> Path:
        return self.run_dir / "protocol_state.json"

    @property
    def events_path(self) -> Path:
        return self.run_dir / "protocol_events.jsonl"

    def save_state(self, state: ProtocolState) -> Path:
        """Persist the protocol state atomically."""
        now = datetime.now(UTC).astimezone().isoformat()
        state.updated_at = now
        state.created_at = state.created_at or now
        _atomic_json(self.state_path, _mask_value(state.to_dict()))
        return self.state_path

    def load_state(self) -> ProtocolState | None:
        """Load protocol state from disk, or None if not found."""
        if not self.state_path.exists():
            return None
        data = json.loads(self.state_path.read_text(encoding="utf-8"))
        return ProtocolState.from_dict(data)

    def append_event(self, event_type: str, **payload: Any) -> Path:
        """Append a timestamped event to the protocol event log."""
        event = {
            "timestamp": datetime.now(UTC).astimezone().isoformat(),
            "type": event_type,
            **payload,
        }
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_mask_value(event), ensure_ascii=False) + "\n")
        return self.events_path

    def write_output(self, stage_key: str, agent: str, content: str) -> Path:
        """Write agent stage output to a file under the run directory."""
        output_dir = self.run_dir / "protocol_outputs" / stage_key
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{agent}.md"
        path.write_text(mask_sensitive(content), encoding="utf-8")
        return path
