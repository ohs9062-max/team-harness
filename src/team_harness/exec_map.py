"""Conservative mapping of shell commands to managed execution kinds."""

from pathlib import Path
import re
import shlex
import tomllib


def split_background(command: str) -> tuple[str, bool]:
    """Remove an unquoted trailing shell background operator."""
    end = len(command.rstrip())
    if not end or command[end - 1] != "&":
        return command, False
    quote = ""
    escaped = False
    for char in command[: end - 1]:
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif char in "'\"" and (not quote or quote == char):
            quote = "" if quote else char
    if quote or escaped or (end > 1 and command[end - 2] in "&>"):
        return command, False
    return command[: end - 1].rstrip(), True


def classify(command: str, config: Path | None = None) -> str | None:
    return _classify(command, config, 0)


def _classify(command: str, config: Path | None, depth: int) -> str | None:
    command, _ = split_background(command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = [part for token in lexer for part in re.split(r"(\n)", token) if part]
    except ValueError:
        return None
    if not tokens:
        return None
    if any(Path(x).name in {"th-job-start.sh", "th-launch.sh"} for x in tokens):
        return None
    config = config or Path.home() / ".team-harness/exec_map.toml"
    try:
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        rules = data["rules"]
        if not isinstance(rules, list):
            raise ValueError("rules must be an array")
        for rule in rules:
            if rule["kind"] not in ("agent_cli", "experiment"):
                raise ValueError("invalid kind")
            re.compile(rule["pattern"])
    except (OSError, ValueError, KeyError, TypeError, re.error):
        rules = []
    for rule in rules:
        if re.search(rule["pattern"], command):
            return rule["kind"]
    # Split substitution delimiters for classification only; execution uses the original.
    classification = (
        re.sub(r"\$\(|`|[()]", "\n", command)
        if "$(" in command or "`" in command
        else command
    )
    try:
        lexer = shlex.shlex(classification, posix=True, punctuation_chars=";|&\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = [part for token in lexer for part in re.split(r"(\n)", token) if part]
    except ValueError:
        return _classify_substitutions(command, config, depth)
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in (";", "&&", "||", "|", "\n"):
            segments.append([])
        else:
            segments[-1].append(token)
    original = command if len(segments) == 1 else None
    kinds = {_classify_segment(segment, rules, original) for segment in segments}
    if "agent_cli" in kinds:
        return "agent_cli"
    if "experiment" in kinds:
        return "experiment"
    return _classify_substitutions(command, config, depth)


def _classify_substitutions(
    command: str, config: Path | None, depth: int
) -> str | None:
    if depth >= 3:
        return None
    kinds = {_classify(body, config, depth + 1) for body in _substitutions(command)}
    if "agent_cli" in kinds:
        return "agent_cli"
    return "experiment" if "experiment" in kinds else None


def _substitutions(command: str) -> list[str]:
    """Extract complete substitutions outside single quotes, respecting nesting."""
    bodies: list[str] = []

    def scan(start: int, terminator: str = "") -> int:
        quote = ""
        i = start
        parentheses = 0
        while i < len(command):
            char = command[i]
            if char == "\\" and quote != "'":
                i += 2
                continue
            if char == "'" and quote != '"':
                quote = "" if quote else "'"
            elif char == '"' and quote != "'":
                quote = "" if quote else '"'
            elif quote != "'":
                if command.startswith("$(", i):
                    end = scan(i + 2, ")")
                    if end < len(command):
                        if not terminator:
                            bodies.append(command[i + 2 : end])
                        i = end
                    else:
                        return end
                elif char == "`":
                    if terminator == "`":
                        return i
                    end = scan(i + 1, "`")
                    if end < len(command):
                        if not terminator:
                            bodies.append(command[i + 1 : end])
                        i = end
                    else:
                        return end
                elif terminator == ")" and not quote:
                    if char == "(":
                        parentheses += 1
                    elif char == ")":
                        if not parentheses:
                            return i
                        parentheses -= 1
            i += 1
        return i

    scan(0)
    return bodies


def _classify_segment(argv: list[str], rules: list, original: str | None) -> str | None:
    initial = argv
    while argv and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*=", argv[0]):
        argv = argv[1:]
    if argv and Path(argv[0]).name == "nohup":
        argv = argv[1:]
    if argv and Path(argv[0]).name == "env":
        argv = argv[1:]
        while argv and re.match(r"^[a-zA-Z_][a-zA-Z0-9_]*=", argv[0]):
            argv = argv[1:]
    if not argv:
        return None
    segment = original if original is not None and argv is initial else shlex.join(argv)
    matched = [rule["kind"] for rule in rules if re.search(rule["pattern"], segment)]
    if "agent_cli" in matched:
        return "agent_cli"
    exe = Path(argv[0]).name
    if exe == "codex" and len(argv) > 1 and argv[1] == "exec":
        return "agent_cli"
    if exe == "claude" and any(x in ("-p", "--print") for x in argv[1:]):
        return "agent_cli"
    if exe == "agy" and any(x in ("-p", "--print", "--prompt") for x in argv[1:]):
        return "agent_cli"
    launcher = (
        exe in ("python", "python3", "torchrun")
        or (exe == "uv" and len(argv) > 1 and argv[1] == "run")
        or (exe == "accelerate" and len(argv) > 1 and argv[1] == "launch")
    )
    if launcher and any(
        re.search(r"train|eval|evaluate|benchmark|experiment", x, re.I)
        for x in argv[1:]
    ):
        return "experiment"
    return "experiment" if "experiment" in matched else None
