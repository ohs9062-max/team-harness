"""Shared coordinator inbox session identity rules."""

from collections.abc import Mapping


def th_session_key(env: Mapping[str, str]) -> str:
    """Choose the same inherited identity used by inbox readers."""
    return (
        env.get("ORCA_PANE_KEY")
        or env.get("CLAUDE_CODE_SESSION_ID")
        or env.get("TH_SESSION_KEY")
        or ""
    )


def valid_route_key(key: str) -> bool:
    return (
        bool(key)
        and key not in {".", "..", "*"}
        and not any(c in key for c in "/\t\n\r\0")
    )
