"""Conservative mapping of shell commands to managed execution kinds."""

from pathlib import Path
import re
import shlex
import tomllib


def classify(command: str, config: Path | None = None) -> str | None:
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or any(x in command for x in ("\n", ";", "|", "&", "`", "$(")):
        return None
    names = [Path(x).name for x in argv]
    if any(x in {"th-job-start.sh", "th-launch.sh"} for x in names):
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
    exe = names[0]
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
    return None
