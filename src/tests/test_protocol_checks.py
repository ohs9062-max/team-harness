from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from team_harness.protocol.checks import CheckResult
from team_harness.protocol.checks import CheckRunner
from team_harness.protocol.checks import evaluate_checks
from team_harness.protocol.models import CheckStatus


def test_check_result_to_dict() -> None:
    res = CheckResult(
        command=["python3", "--version"],
        success=True,
        exit_code=0,
        stdout="Python 3.12.0\n",
        stderr="",
        duration_sec=0.01,
        timed_out=False,
    )
    d = res.to_dict()
    assert d["command"] == ["python3", "--version"]
    assert d["success"] is True
    assert d["exit_code"] == 0
    assert d["stdout"] == "Python 3.12.0\n"


def test_check_runner_run_one_success_and_failure(tmp_path: Path) -> None:
    runner = CheckRunner(tmp_path)

    # Successful command
    ok_res = runner.run_one([sys.executable, "-c", "print('hello from check')"])
    assert ok_res.success is True
    assert ok_res.exit_code == 0
    assert "hello from check" in ok_res.stdout

    # Failing command
    fail_res = runner.run_one([sys.executable, "-c", "import sys; sys.exit(2)"])
    assert fail_res.success is False
    assert fail_res.exit_code == 2


def test_check_runner_run_one_timeout(tmp_path: Path) -> None:
    runner = CheckRunner(tmp_path, timeout_sec=1)
    res = runner.run_one([sys.executable, "-c", "import time; time.sleep(2)"])
    assert res.success is False
    assert res.timed_out is True
    assert res.exit_code == -2


def test_check_runner_discover_nodejs(tmp_path: Path) -> None:
    package_json = tmp_path / "package.json"
    package_json.write_text(
        json.dumps({"scripts": {"lint": "eslint .", "test": "jest"}}), encoding="utf-8"
    )
    runner = CheckRunner(tmp_path)
    discovered = runner.discover()
    assert ["npm", "run", "lint"] in discovered
    assert ["npm", "run", "test"] in discovered


def test_check_runner_discover_python(tmp_path: Path) -> None:
    test_dir = tmp_path / "tests"
    test_dir.mkdir()
    (test_dir / "test_example.py").write_text("def test_ok(): pass\n", encoding="utf-8")

    runner = CheckRunner(tmp_path)
    discovered = runner.discover()
    assert len(discovered) >= 1
    assert any("test_*.py" in arg for cmd in discovered for arg in cmd)


def test_parse_user_command_safety() -> None:
    assert CheckRunner.parse_user_command("pytest -q") == ["pytest", "-q"]

    # Disallowed shell control operators
    with pytest.raises(ValueError, match="Shell control operators are not supported"):
        CheckRunner.parse_user_command("pytest && rm -rf /")

    with pytest.raises(ValueError, match="Shell control operators are not supported"):
        CheckRunner.parse_user_command("pytest; echo done")

    with pytest.raises(ValueError, match="Shell control operators are not supported"):
        CheckRunner.parse_user_command("pytest | grep error")


def test_evaluate_checks_waived_when_no_commands() -> None:
    """When no check commands exist, status MUST be WAIVED, never PASS."""
    assert evaluate_checks([]) == CheckStatus.WAIVED
    assert evaluate_checks(None) == CheckStatus.NOT_CONFIGURED


def test_evaluate_checks_pass_and_fail() -> None:
    pass_res = CheckResult(
        command=["test"],
        success=True,
        exit_code=0,
        stdout="",
        stderr="",
        duration_sec=0.1,
    )
    fail_res = CheckResult(
        command=["lint"],
        success=False,
        exit_code=1,
        stdout="",
        stderr="error",
        duration_sec=0.1,
    )

    assert evaluate_checks([pass_res]) == CheckStatus.PASS
    assert evaluate_checks([pass_res, pass_res]) == CheckStatus.PASS
    assert evaluate_checks([pass_res, fail_res]) == CheckStatus.FAIL
    assert evaluate_checks([fail_res]) == CheckStatus.FAIL
