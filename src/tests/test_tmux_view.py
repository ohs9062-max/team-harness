# pyright: reportMissingParameterType=false

"""Tests for the optional tmux worker-viewer (TH-D12).

All `tmux` invocations are mocked: these tests verify the shell-out shape and
the fail-soft contract, not real tmux behavior (that's exercised manually /
in the demo, not in CI where tmux may be absent).
"""

from unittest.mock import MagicMock

import pytest

from team_harness.agents.tmux_view import sanitize_window_name
from team_harness.agents.tmux_view import tmux_available
from team_harness.agents.tmux_view import TmuxViewer


def test_tmux_available_reflects_path_lookup(monkeypatch):
    monkeypatch.setattr(
        "team_harness.agents.tmux_view.shutil.which", lambda name: "/usr/bin/tmux"
    )
    assert tmux_available() is True

    monkeypatch.setattr("team_harness.agents.tmux_view.shutil.which", lambda name: None)
    assert tmux_available() is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("codex_ab12cd34", "codex_ab12cd34"),
        ("worker 1!!", "worker-1"),
        ("///", "worker"),
        ("", "worker"),
    ],
)
def test_sanitize_window_name(raw, expected):
    assert sanitize_window_name(raw) == expected


@pytest.mark.asyncio
async def test_ensure_session_creates_only_when_missing(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 1 if cmd[1] == "has-session" else 0
        return result

    monkeypatch.setattr("team_harness.agents.tmux_view.subprocess.run", fake_run)

    viewer = TmuxViewer("demo")
    await viewer.ensure_session()

    assert calls == [
        ["tmux", "has-session", "-t", "demo"],
        ["tmux", "new-session", "-d", "-s", "demo", "-n", "coordinator"],
    ]


@pytest.mark.asyncio
async def test_ensure_session_is_a_no_op_when_session_exists(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr("team_harness.agents.tmux_view.subprocess.run", fake_run)

    await TmuxViewer("demo").ensure_session()

    assert calls == [["tmux", "has-session", "-t", "demo"]]


@pytest.mark.asyncio
async def test_open_log_window_tails_the_log_path(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        result = MagicMock()
        result.returncode = 0
        return result

    monkeypatch.setattr("team_harness.agents.tmux_view.subprocess.run", fake_run)
    log_path = tmp_path / "codex_ab12_stdout.log"

    await TmuxViewer("demo").open_log_window(
        window_name="codex_ab12", log_path=log_path
    )

    # First call is the has-session probe from ensure_session, second opens
    # the window; the worker's stdin is never referenced anywhere here.
    assert calls[0] == ["tmux", "has-session", "-t", "demo"]
    new_window_call = calls[-1]
    assert new_window_call[:5] == ["tmux", "new-window", "-t", "demo", "-n"]
    assert new_window_call[5] == "codex_ab12"
    assert str(log_path) in new_window_call[6]
    assert new_window_call[6].startswith("tail ")


@pytest.mark.asyncio
async def test_open_log_window_never_raises_when_tmux_disappears_mid_run(
    monkeypatch, tmp_path
):
    """tmux_available() only checks at construction time; tmux can still

    vanish between that check and a later call (uninstalled, PATH changed).
    A broken view must never take the worker run down with it.
    """

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError("tmux not found")

    monkeypatch.setattr("team_harness.agents.tmux_view.subprocess.run", fake_run)

    # Must not raise.
    await TmuxViewer("demo").open_log_window(
        window_name="x", log_path=tmp_path / "x.log"
    )


def test_attach_hint_names_the_session():
    assert TmuxViewer("team-harness-abc123").attach_hint() == (
        "tmux attach -t team-harness-abc123"
    )
