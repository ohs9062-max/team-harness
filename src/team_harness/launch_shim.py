"""Best-effort observation before direct CLI exec; never enforce decisions."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import sys


def _option(args: list[str], *names: str) -> str:
    for i, arg in enumerate(args):
        for name in names:
            if arg == name and i + 1 < len(args):
                return args[i + 1]
            if arg.startswith(name + "="):
                return arg[len(name) + 1 :]
    return ""


def _prompt(tool: str, args: list[str]) -> str | None:
    if tool in {"agy", "claude"}:
        return _option(args, "-p", "--prompt") or None
    # Only inspect an unambiguous final positional prompt. Option values and
    # stdin are never read or mistaken for a brief.
    if (
        not args
        or args[-1] in {"-", "exec", "resume"}
        or (args[-1].startswith("-") and "\n" not in args[-1])
    ):
        return None
    options_with_values = {
        "-m",
        "--model",
        "-c",
        "--config",
        "-C",
        "--cd",
        "-o",
        "--output-last-message",
        "--sandbox",
        "-s",
        "--profile",
        "-p",
        "--output-schema",
        "--image",
        "-i",
        "--add-dir",
        "--color",
    }
    if len(args) >= 2 and args[-2] in options_with_values:
        return None
    return args[-1]


def _effort(args: list[str]) -> str:
    direct = _option(args, "--effort")
    if direct:
        return direct
    for i, arg in enumerate(args):
        if arg in {"-c", "--config"} and i + 1 < len(args):
            value = args[i + 1]
        elif arg.startswith("--config="):
            value = arg[len("--config=") :]
        else:
            continue
        key, sep, value = value.partition("=")
        if sep and key.strip() == "model_reasoning_effort":
            return value.strip().strip("\"'")
    return ""


def _run_pipeline(
    tool: str, args: list[str], depth: int, pid: int | None = None
) -> list[str]:
    from team_harness.agents.process_identity import capture_starttime
    from team_harness.exec_pipeline import ExecEvent
    from team_harness.exec_pipeline import run_pre

    event = ExecEvent(
        tool=tool,
        role=os.environ.get("TH_ROLE", ""),
        session=os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        cwd=os.getcwd(),
        task=os.environ.get("TH_WORKER_OUTPUT_DIR")
        or os.environ.get("TH_JOB_TASK")
        or os.getcwd(),
        owner=os.environ.get("TH_WORKSPACE", ""),
        argv=args,
        depth=depth,
        pid=pid if pid is not None else os.getpid(),
        process_start=capture_starttime(pid if pid is not None else os.getpid()) or "",
    )
    run_pre(event)
    return event.warnings


def main(tool: str) -> None:
    shim_dir = Path(sys.argv[0]).absolute().parent.resolve()
    path = os.pathsep.join(
        p
        for p in os.environ.get("PATH", "").split(os.pathsep)
        if Path(p or ".").resolve() != shim_dir
        and (Path(p or ".") / tool).resolve() != Path(sys.argv[0]).resolve()
    )
    real = shutil.which(tool, path=path)
    if real is None:
        print(f"{tool}: command not found", file=sys.stderr)
        raise SystemExit(127)
    args = sys.argv[1:]
    noninteractive = (
        (
            "exec" in args
            and ("resume" not in args or args.index("exec") < args.index("resume"))
        )
        if tool == "codex"
        else bool(_option(args, "-p", "--prompt")) or not sys.stdin.isatty()
    )
    if os.environ.get("TH_SHIM") != "0" and noninteractive:
        try:
            from team_harness.proc_observer import has_ancestor_command

            if not has_ancestor_command("th-launch.sh"):
                depth = int(os.environ.get("TH_SHIM_DEPTH", "0")) + 1
                os.environ["TH_SHIM_DEPTH"] = str(depth)
                warnings = _run_pipeline(tool, args, depth)
                if os.environ.get("TH_SHIM_QUIET") != "1":
                    for warning in warnings[:5]:
                        print(f"[th] {warning}", file=sys.stderr)
                    if len(warnings) > 5:
                        print(f"[th] 외 {len(warnings) - 5}건", file=sys.stderr)
        except Exception:
            pass
    os.execv(real, [real, *args])


def observe_child() -> None:
    """Observe in an expendable subprocess, without consuming caller input."""
    tool, tty, *args = sys.argv[1:]
    noninteractive = (
        (
            "exec" in args
            and ("resume" not in args or args.index("exec") < args.index("resume"))
        )
        if tool == "codex"
        else bool(_option(args, "-p", "--prompt")) or tty == "0"
    )
    if not noninteractive:
        return
    from team_harness.proc_observer import has_ancestor_command

    if has_ancestor_command("th-launch.sh"):
        return
    depth = int(os.environ.get("TH_SHIM_DEPTH", "0")) + 1
    warnings = _run_pipeline(tool, args, depth, int(os.environ["TH_SHIM_PARENT_PID"]))
    print(depth)
    if os.environ.get("TH_SHIM_QUIET") != "1":
        for warning in warnings[:5]:
            print(f"[th] {warning}")
        if len(warnings) > 5:
            print(f"[th] 외 {len(warnings) - 5}건")
