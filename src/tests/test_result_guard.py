"""Tests for exact result parsing and task failure precedence."""

from pathlib import Path

import pytest

from team_harness.result_guard import read_text
from team_harness.result_guard import result_code
from team_harness.result_guard import task_code
from team_harness.result_guard import task_key


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("결과: 0", 0),
        ("결과: 1\nbody", 1),
        ("결과: 2\n", 2),
        (" 결과: 0", 1),
        ("결과: 0 ", 1),
        ("결과:0", 1),
        ("결과: 3", 1),
        ("heading\n결과: 0", 1),
        ("", 1),
    ],
)
def test_exact_result_codes(text: str, expected: int) -> None:
    assert result_code(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("entry | 결과:0\n", 0),
        ("entry | 결과:1", 1),
        ("entry | 결과:2", 2),
        ("entry | 결과:0 ", 1),
        ("entry | 결과: 0", 1),
        ("entry | 결과:0| other", 1),
        ("", 1),
    ],
)
def test_journal_result_codes(text: str, expected: int) -> None:
    assert result_code(text, journal=True) == expected


@pytest.mark.parametrize("marker", ["FAILED", "FAILED.x", "VERIFY_FAILED"])
def test_failure_markers_override_report(tmp_path: Path, marker: str) -> None:
    (tmp_path / "REPORT.md").write_text("결과: 0", encoding="utf-8")
    (tmp_path / marker).touch()
    assert task_code(tmp_path) == 2


def test_verification_and_missing_report(tmp_path: Path) -> None:
    assert task_code(tmp_path) == 1
    report = tmp_path / "REPORT.md"
    report.write_text("", encoding="utf-8")
    assert task_code(tmp_path) == 1
    report.write_text("결과: 0", encoding="utf-8")
    verification = tmp_path / "verify_result.json"
    verification.write_text('{"overall": "FAIL"}', encoding="utf-8")
    assert task_code(tmp_path) == 2
    verification.write_text("{broken", encoding="utf-8")
    assert task_code(tmp_path) == 0
    assert task_code(tmp_path, failed=True) == 2
    assert result_code("결과: 0", failed=True) == 2


def test_read_text_and_task_key(tmp_path: Path) -> None:
    assert read_text(tmp_path / "missing") == ""
    assert task_key(tmp_path) == str(tmp_path)
    (tmp_path / "task.md").write_text(
        '---\ntask_key: "example"\n---\n', encoding="utf-8"
    )
    assert task_key(tmp_path) == "example"
