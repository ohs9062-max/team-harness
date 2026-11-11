"""Recognize only standard managed hook commands; preserve user shell programs.

Standalone installers use this stdlib-only module with system Python. Shell
expansions and unknown argv/environment changes are deliberately left untouched.
"""

from collections.abc import Iterable
from collections.abc import Mapping
import os
from pathlib import Path
import re
import shlex

_ASSIGNMENT = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)=(.*)", re.DOTALL)
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?")
_SHELLS = {"sh", "bash", "dash", "zsh", "ksh"}


def _invocation(
    command: str, allowed_env: Mapping[str, str]
) -> tuple[str, tuple[str, ...]] | None:
    # shlex removes quoting but does not interpret shell expansion. Reject it
    # before tokenization, even inside quotes, rather than deleting side effects
    # hidden in an otherwise standard managed script invocation.
    if any(
        marker in command for marker in ("\n", "\r", "\\", "`", "$(", "$[", "*", "?")
    ):
        return None
    home_only = command.replace("${HOME}/", "").replace("$HOME/", "")
    if "$" in home_only or re.search(r"'[^']*[$~][^']*'|\"~/", command):
        return None
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        args = list(lexer)
    except ValueError:
        return None
    if not args or any(token and set(token) <= set(";&|()<>") for token in args):
        return None

    def consume_assignments() -> bool:
        while args and (match := _ASSIGNMENT.fullmatch(args[0])):
            if allowed_env.get(match[1]) != match[2]:
                return False
            args.pop(0)
        return True

    if not consume_assignments():
        return None
    if args and args[0] in {"env", "/usr/bin/env", "/bin/env"}:
        args.pop(0)
        if not consume_assignments():
            return None
    if not args:
        return None
    invoked = args.pop(0)
    executable = Path(invoked).name
    if _PYTHON.fullmatch(executable) or executable in _SHELLS:
        if invoked != executable and Path(invoked).parent not in {
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/local/bin"),
        }:
            return None
        # Standard installers use plain interpreters. Flags (-c/-m/-n/-I,
        # env -i/-u, etc.) are user variants and must not be rewritten.
        if not args or args[0].startswith(("-", "+")):
            return None
        invoked = args.pop(0)
    return invoked, tuple(args)


def _path(value: str) -> str:
    if value.startswith("${HOME}/"):
        value = str(Path.home()) + value[len("${HOME}") :]
    elif value.startswith("$HOME/"):
        value = str(Path.home()) + value[len("$HOME") :]
    return os.path.normpath(os.path.expanduser(value))


def managed_hook(
    item: Mapping[str, object],
    scripts: Iterable[str | Path],
    *,
    allowed_args: Iterable[tuple[str, ...]] = ((),),
    allowed_env: Mapping[str, str] | None = None,
) -> bool:
    if item.get("type") != "command" or not isinstance(item.get("command"), str):
        return False
    invocation = _invocation(str(item["command"]), allowed_env or {})
    if invocation is None:
        return False
    script, arguments = invocation
    return arguments in allowed_args and _path(script) in {
        _path(str(path)) for path in scripts
    }


def deployed_paths(name: str) -> list[Path]:
    """Known hook directory, including the live HOME during staged installation."""
    homes = {
        Path.home(),
        Path(os.environ.get("TH_INSTALL_TARGET_HOME", str(Path.home()))),
    }
    return [home / ".claude/hooks" / name for home in homes]
