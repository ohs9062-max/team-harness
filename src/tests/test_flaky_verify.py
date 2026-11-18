from pathlib import Path
import sys

import pytest

from team_harness.stop_policy import classify_stop
from team_harness.worker_digest import verify


def test_failure_evidence(tmp_path: Path) -> None:
    (tmp_path / "test_sample.py").write_text("def test_failure():\n    assert False\n")
    (tmp_path / "verify.sh").write_text(
        f"{sys.executable} -m pytest -q test_sample.py\n"
    )
    (tmp_path / "DONE").write_text("done")
    result, _ = verify(tmp_path, force=True)
    check = result["items"]["verify"]
    assert result["overall"] == "FAIL"
    assert "test_sample.py::test_failure" in check["reason"]
    assert "실패 명령:" not in check["reason"]
    assert (
        "FAILED test_sample.py::test_failure"
        in (tmp_path / "verify_output.log").read_text()
    )
    assert classify_stop(tmp_path) == "self_stop"


def test_verify_does_not_inherit_trace_fd(tmp_path: Path) -> None:
    (tmp_path / "verify.sh").write_text("test ! -e /proc/$$/fd/9\n")
    (tmp_path / "DONE").write_text("done")
    result, _ = verify(tmp_path, force=True)
    assert result["overall"] == "PASS"
    assert (tmp_path / "verify_output.log").read_text() == ""


def test_stderr_preserved(tmp_path: Path) -> None:
    (tmp_path / "verify.sh").write_text("echo error >&2\nexit 1\n")
    (tmp_path / "DONE").write_text("done")
    result, _ = verify(tmp_path, force=True)
    assert result["overall"] == "FAIL"
    assert "error" in (tmp_path / "verify_output.log").read_text()


@pytest.mark.parametrize("case", ["export", "ignored", "inverted"])
def test_shell_counterexamples_run_once(tmp_path: Path, case: str) -> None:
    (tmp_path / "test_fail.py").write_text("def test_fail():\n    assert False\n")
    (tmp_path / "test_pass.py").write_text("def test_pass():\n    assert True\n")
    pytest_cmd = f"{sys.executable} -m pytest -q"
    scripts = {
        "export": f"export PROBE=present\n{pytest_cmd} test_fail.py\n",
        "ignored": f"{pytest_cmd} test_fail.py || true\nfalse\n",
        "inverted": f"{pytest_cmd} test_fail.py || true\n! {pytest_cmd} test_pass.py\n",
    }
    (tmp_path / "verify.sh").write_text("echo run >> runs\n" + scripts[case])
    (tmp_path / "DONE").write_text("done")
    result, _ = verify(tmp_path, force=True)
    assert result["overall"] == "FAIL"
    assert "classification" not in result["items"]["verify"]
    assert (tmp_path / "runs").read_text().splitlines() == ["run"]
    assert not (tmp_path / "verify_retry_output.log").exists()
    assert (tmp_path / "verify_output.log").exists()
