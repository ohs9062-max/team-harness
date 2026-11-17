"""Status rendering is importable without producing output."""

import pytest


def test_status_module_import(capsys: pytest.CaptureFixture[str]) -> None:
    from team_harness import status

    assert callable(status.main)
    assert capsys.readouterr().out == ""
    assert capsys.readouterr().err == ""
