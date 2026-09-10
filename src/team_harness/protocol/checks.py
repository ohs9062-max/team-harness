"""Deterministic checks executed before an LLM review.

CHECK is a System stage — not an LLM judgment.  When no check commands
exist the result is WAIVED, never a false PASS.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from dataclasses import dataclass
import json
from pathlib import Path
import shlex
import subprocess
import sys
import time

from team_harness.protocol.models import CheckStatus


@dataclass
class CheckResult:
    command: list[str]
    success: bool
    exit_code: int
    stdout: str
    stderr: str
    duration_sec: float
    timed_out: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_checks(results: Sequence[CheckResult] | None) -> CheckStatus:
    """Evaluate a list of CheckResult objects.

    Returns:
        CheckStatus.NOT_CONFIGURED if results is None.
        CheckStatus.WAIVED if results is empty list (no checks found).
        CheckStatus.PASS if all checks succeeded.
        CheckStatus.FAIL if any check failed.
    """
    if results is None:
        return CheckStatus.NOT_CONFIGURED
    if not results:
        return CheckStatus.WAIVED
    if all(r.success for r in results):
        return CheckStatus.PASS
    return CheckStatus.FAIL


class CheckRunner:
    """Discovers project checks and runs them without a shell."""

    def __init__(
        self,
        repo_root: str | Path,
        timeout_sec: int = 300,
        max_output_chars: int = 80_000,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.timeout_sec = timeout_sec
        self.max_output_chars = max_output_chars

    def discover(self) -> list[list[str]]:
        """Auto-discover conservative project check commands."""
        commands: list[list[str]] = []

        # Node.js projects
        package_json = self.repo_root / "package.json"
        if package_json.exists():
            try:
                scripts = json.loads(package_json.read_text(encoding="utf-8")).get(
                    "scripts", {}
                )
            except (OSError, json.JSONDecodeError, AttributeError):
                scripts = {}
            for script in ("lint", "typecheck", "test", "build"):
                if script in scripts:
                    commands.append(["npm", "run", script])

        # Python projects
        excluded_parts = {".git", ".harness", ".venv", "venv", "node_modules"}
        test_roots = sorted(
            {
                path.parent
                for path in self.repo_root.glob("**/tests/test_*.py")
                if not (set(path.relative_to(self.repo_root).parts) & excluded_parts)
            }
        )
        for test_root in test_roots:
            commands.append(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    str(test_root.relative_to(self.repo_root)),
                    "-p",
                    "test_*.py",
                    "-v",
                ]
            )
        if not test_roots and (self.repo_root / "pyproject.toml").exists():
            commands.append([sys.executable, "-m", "pytest", "-q"])

        return commands

    def run_all(self, commands: Sequence[Sequence[str]]) -> list[CheckResult]:
        return [self.run_one(list(cmd)) for cmd in commands]

    def run_one(self, command: list[str]) -> CheckResult:
        """Run a single check command and return the result."""
        started = time.monotonic()
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                check=False,
                shell=False,
            )
            return CheckResult(
                command=command,
                success=result.returncode == 0,
                exit_code=result.returncode,
                stdout=result.stdout[: self.max_output_chars],
                stderr=result.stderr[: self.max_output_chars],
                duration_sec=time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as error:
            return CheckResult(
                command=command,
                success=False,
                exit_code=-2,
                stdout=(
                    (error.stdout or "")[: self.max_output_chars]
                    if isinstance(error.stdout, str)
                    else ""
                ),
                stderr=(
                    (error.stderr or "")[: self.max_output_chars]
                    if isinstance(error.stderr, str)
                    else ""
                ),
                duration_sec=time.monotonic() - started,
                timed_out=True,
            )
        except OSError as error:
            return CheckResult(
                command=command,
                success=False,
                exit_code=-3,
                stdout="",
                stderr=str(error),
                duration_sec=time.monotonic() - started,
            )

    @staticmethod
    def parse_user_command(value: str) -> list[str]:
        """Parse a user-supplied check command string into argv."""
        forbidden_chars = {";", "&", "|", ">", "<", "`", "$"}
        if any(char in value for char in forbidden_chars):
            raise ValueError(
                "Shell control operators are not supported in check commands"
            )
        command = shlex.split(value)
        if not command:
            raise ValueError("Empty check command")
        return command
