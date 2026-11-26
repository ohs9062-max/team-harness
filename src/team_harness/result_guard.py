"""Exact task result code parsing shared by harness entry points."""

import json
from pathlib import Path


def result_code(text: str, *, journal: bool = False, failed: bool = False) -> int:
    """Return the exact reported result, giving failure markers precedence."""
    if failed:
        return 2
    if journal:
        field = text.rstrip("\r\n").split("|")[-1]
        return int(field[-1]) if field in (" 결과:0", " 결과:1", " 결과:2") else 1
    lines = text.splitlines()
    first = lines[0] if lines else ""
    return int(first[-1]) if first in ("결과: 0", "결과: 1", "결과: 2") else 1


def read_text(path: Path) -> str:
    """Read a task file, treating inaccessible files as empty."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def task_code(task: Path, *, failed: bool = False) -> int:
    """Read the task report and failure markers."""
    failed = failed or (task / "FAILED").exists() or bool(list(task.glob("FAILED.*")))
    failed = failed or (task / "VERIFY_FAILED").exists()
    try:
        verification = json.loads(read_text(task / "verify_result.json"))
        failed = failed or verification.get("overall") == "FAIL"
    except (ValueError, AttributeError):
        pass
    return result_code(read_text(task / "REPORT.md"), failed=failed)


def task_key(task: Path) -> str:
    """Read the task key from front matter, falling back to its path."""
    lines = read_text(task / "task.md").splitlines()
    if not lines or lines[0].strip() != "---":
        return str(task)
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep and key == "task_key" and value.strip():
            return value.strip().strip("\"'")
    return str(task)
