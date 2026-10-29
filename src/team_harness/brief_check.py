"""Conservative preflight checks for worker briefs."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import re
import shlex
import shutil
import sys
from typing import Literal


@dataclass(frozen=True)
class Finding:
    level: Literal["error", "warn"]
    rule: str
    line: int
    message: str


def check_brief_text(text: str, task_dir: Path | None = None) -> list[Finding]:
    lines = text.splitlines()
    findings: list[Finding] = []
    front_end = (
        next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        if lines and lines[0].strip() == "---"
        else 0
    )
    front = "\n".join(lines[1:front_end])
    code = bool(re.search(r"^kind:\s*code\s*$", front, re.M))
    if re.search(r"^allow_paths\s*:", front, re.M) and not re.search(
        r"작업\s*트리.*(?:전체|전부).*이\s*작업\s*폴더.*쓰기\s*허용", text
    ):
        findings.append(
            Finding(
                "warn", "allow_paths_ambiguous", 1, "기본 쓰기 허용 범위를 명시하세요"
            )
        )
    acceptance = False
    in_acceptance = False
    in_bash = False
    in_read_section = False
    in_tasks = False
    commands: list[str] = []
    own = task_dir.name if task_dir else None
    for i, line in enumerate(lines, 1):
        if i <= front_end + 1 and front_end:
            continue
        if re.match(r"^##\s+", line):
            in_acceptance = bool(re.match(r"^##\s+검수\s*$", line))
            in_read_section = bool(re.match(r"^##\s+읽을\s*문서", line))
            in_tasks = bool(re.match(r"^##\s+할\s*일", line))
            acceptance |= in_acceptance
            in_bash = False
        if in_acceptance and re.match(r"^```(?:bash|sh)\s*$", line):
            in_bash = True
            continue
        if in_bash and line.startswith("```"):
            in_bash = False
            continue
        if in_bash:
            commands.append(line)
            # Split shell pipelines and compound commands; skip shell constructs and uv run.
            for part in re.split(r"\s*(?:&&|\|\||[|;])\s*", line):
                try:
                    words = shlex.split(part, comments=True)
                except ValueError:
                    continue
                while words and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", words[0]):
                    words.pop(0)
                while words and words[0] in {"{", "}", "(", ")", "!"}:
                    words.pop(0)
                if not words or words[0] in {
                    "cd",
                    "test",
                    "[",
                    "echo",
                    "printf",
                    "export",
                    "set",
                    "if",
                    "then",
                    "fi",
                    "for",
                    "do",
                    "done",
                    "true",
                    "false",
                    "local",
                    "return",
                    "exit",
                    "command",
                    "[[",
                    "]]",
                    "while",
                    "until",
                    "case",
                    "esac",
                    "elif",
                    "else",
                    "in",
                    "break",
                    "continue",
                    "readonly",
                    "unset",
                    "shift",
                    "trap",
                    "eval",
                    "exec",
                    "source",
                    ".",
                }:
                    continue
                if words[0] == "uv" and len(words) > 1 and words[1] == "run":
                    continue
                if not shutil.which(words[0]):
                    findings.append(
                        Finding(
                            "error",
                            "missing_command",
                            i,
                            f"명령을 찾을 수 없음: {words[0]}",
                        )
                    )
        escape = re.search(
            r"완료한\s*단계까지|끝낸\s*단계까지|남은\s*항목.{0,30}DONE|DONE.{0,30}남은\s*항목|일부만|INCOMPLETE로",
            line,
        )
        if escape and not re.search(
            r"(?:하지\s*않|금지)", line[escape.end() : escape.end() + 15]
        ):
            findings.append(
                Finding("warn", "escape_phrase", i, "부분 완료 허용 문구를 확인하세요")
            )
        if re.search(
            r"읽지\s*(?:않|말)|금지|하지\s*않|건드리지\s*않|가드가\s*막", line
        ):
            continue
        if "worktree add" in line:
            continue
        level: Literal["error", "warn"] = (
            "error"
            if in_read_section
            or in_tasks
            and re.search(
                r"읽|\bread\b|\b(?:cat|tail|head|rg|grep)(?:\s|`)|\bsed\s+-n\b|\bopen\s*\(",
                line,
                re.I,
            )
            else "warn"
        )
        if re.search(r"\bcat\s*>>?", line) and not re.search(
            r"읽|\bread\b|\b(?:tail|head|rg|grep)\s", line, re.I
        ):
            level = "warn"
        paths = re.findall(
            r"(?:~\/|[\w./<>-]*/)?\.team-harness/watch(?:/[\w.*/-]+)?|(?:[\w./<>-]*/)?_outputs/([^/\s`]+)(?:/[^\s`]*)?|(?:<[^>]+>|/[^\s`]*)/\.claude/[^\s`]*",
            line,
        )
        sibling = False
        if task_dir is not None:
            parent = re.escape(str(task_dir.parent))
            for match in re.finditer(parent + r"/([^/\s`]+)(?:/[^\s`]*)?", line):
                if match.group(1) != own:
                    sibling = True
        if sibling:
            findings.append(
                Finding(level, "foreign_path", i, "다른 작업 폴더를 확인하세요")
            )
            continue
        # References to job-output paths in examples are not necessarily read requests.
        if re.search(r"(?:\.team-harness/watch|(?:<[^>]+>|/[^\s`]*)/\.claude/)", line):
            findings.append(
                Finding(level, "foreign_path", i, "보호된 외부 경로를 확인하세요")
            )
        elif paths:
            names = [p for p in paths if p and p not in {"*", "<실제 작업>"}]
            if any(name != own for name in names) and not re.search(
                r"(?:TH_JOB_TASK|TH_WORKER_OUTPUT_DIR)\s*=", line
            ):
                findings.append(
                    Finding(level, "foreign_path", i, "다른 작업 폴더를 확인하세요")
                )
    if code and not acceptance:
        findings.append(Finding("warn", "missing_acceptance", 1, "검수 절이 없습니다"))
    elif code and commands:
        checks = "\n".join(commands)
        if (
            re.search(r"git\s+log", checks)
            and re.search(r"\bgrep\b", checks)
            and not re.search(
                r"\b(?:pytest|test|\[|stat|curl|python|node|cargo|make)\b", checks
            )
        ):
            findings.append(
                Finding(
                    "warn",
                    "weak_acceptance",
                    1,
                    "커밋 로그 외의 동작 검수가 필요합니다",
                )
            )
    return findings


def check_brief(task_dir: Path) -> list[Finding]:
    task_dir = Path(task_dir)
    return check_brief_text(
        (task_dir / "task.md").read_text(encoding="utf-8"), task_dir
    )


def log_brief_warnings(text: str) -> None:
    for finding in check_brief_text(text):
        logging.getLogger(__name__).warning(
            "brief check %s:%s: %s", finding.rule, finding.line, finding.message
        )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: python -m team_harness.brief_check <task_dir>", file=sys.stderr)
        return 1
    findings = check_brief(Path(sys.argv[1]))
    for item in findings:
        print(f"{item.level}: {item.rule}:{item.line}: {item.message}", file=sys.stderr)
    return 2 if any(item.level == "error" for item in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
