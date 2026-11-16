"""Conservative detection of application-path requirements."""

from __future__ import annotations

import re
import subprocess
import sys


def paths_reason(task_md: str) -> str | None:
    front_match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", task_md, re.S)
    if not front_match:
        return None
    front = front_match.group(1)
    if not re.search(r'(?m)^kind:\s*["\']?code["\']?\s*$', front):
        return None
    if re.search(r"(?im)^paths:\s*(?:th-only|n/a)\s+\S", front):
        return None
    if not re.search(
        r"(?im)(?:작업\s*트리|worktree|저장소|repository|repo\b)[^\n]*(?:\n(?!#)[^\n]*)?team-harness|git\s+-C\s+\S*team-harness",
        task_md,
    ):
        return None
    # Only explicit feature/change language is sufficient; ambiguous briefs are exempt.
    if not re.search(
        r"기능\s*(?:추가|구현|확장|변경)|동작\s*변경|(?:add|implement|introduce|extend)\s+(?:a\s+|the\s+)?feature|behavior\s+change",
        task_md,
        re.I,
    ):
        return None
    return "team-harness 코드 기능 추가·동작 변경"


def needs_paths(task_md: str) -> bool:
    return paths_reason(task_md) is not None


def _paths_tables(text: str) -> list[list[str]]:
    tables = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "적용 경로" not in line:
            continue
        if line.lstrip().startswith("|"):
            start = i
        elif re.match(r"^\s*#{1,6}\s+", line):
            start = i + 1
        else:
            continue
        block = []
        for following in lines[start:]:
            if re.match(r"^\s*#{1,6}\s+", following):
                break
            block.append(following)
        if any(re.match(r"^\s*\|\s*:?-{3,}", row) for row in block):
            tables.append(block)
    return tables


def has_paths_table(text: str) -> bool:
    return any(
        "th 밖" in row
        for block in _paths_tables(text)
        for row in block
        if row.lstrip().startswith("|")
    )


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
    return result.stdout if result.returncode == 0 else ""


def push_violations(revisions: list[str]) -> list[str]:
    violations = []
    for revision in revisions:
        parent = _git("show", f"{revision}^:design/decisions.md")
        current = _git("show", f"{revision}:design/decisions.md")
        old = set(re.findall(r"(?m)^##\s+TH-D(\d+)(?:[.\s]|$)", parent))
        sections = re.split(r"(?m)(?=^##\s+TH-D\d+(?:[.\s]|$))", current)
        for section in sections:
            match = re.match(r"##\s+TH-D(\d+)(?:[.\s]|$)", section)
            if match and match.group(1) not in old and not has_paths_table(section):
                violations.append(f"{revision[:12]} TH-D{match.group(1)}")
        names = _git(
            "diff-tree",
            "--root",
            "-m",
            "--no-commit-id",
            "--name-only",
            "-r",
            revision,
            "--",
            "design/designs/",
        ).splitlines()
        for name in set(names):
            if not name.endswith(".md"):
                continue
            content = _git("show", f"{revision}:{name}")
            if _paths_tables(content) and not has_paths_table(content):
                violations.append(f"{revision[:12]} {name}")
    return violations


if __name__ == "__main__":
    errors = push_violations(sys.argv[1:])
    if errors:
        print("\n".join(errors), file=sys.stderr)
        sys.exit(1)
