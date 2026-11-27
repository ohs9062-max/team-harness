#!/usr/bin/env python3
"""Measure deployed execution adapters in an isolated temporary workspace."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

DEPLOYED = Path("/home/hs/rang/team-harness")
SHIM = Path("/home/hs/.team-harness/shim/bin")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}

    def record(result: str, cell: str, detail: str) -> None:
        counts[result] += 1
        print(f"{result} {cell} {detail}", flush=True)

    with tempfile.TemporaryDirectory(prefix="th-intercept-check-") as tmp:
        base = Path(tmp)
        home, watch, work, fake = (base / x for x in ("home", "watch", "work", "bin"))
        for directory in (home, watch, work, fake, work / "x"):
            directory.mkdir()
        (work / "train.py").write_text(
            'import os, sys\nprint("ok")\nsys.exit(int(os.getenv("FAKE_EXIT", "0")))\n'
        )
        (work / "x" / "train.py").write_text('print("ok")\n')
        for name in ("codex", "agy", "claude", "th-job-start.sh"):
            path = fake / name
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        env = {k: v for k, v in os.environ.items() if not k.startswith("TH_")}
        env.update(
            HOME=str(home),
            TH_WATCH_DIR=str(watch),
            TH_NO_DAEMON="1",
            TH_HARNESS_SRC=str(DEPLOYED / "src"),
            PATH=f"{fake}:/usr/bin:/bin",
        )
        hook = DEPLOYED / "scripts/hooks/th-exec-pipeline.py"
        commands = {
            "simple": "python3 train.py",
            "cd": "cd x && python3 train.py",
            "pipe": "python3 train.py | tail",
            "background": "python3 train.py &",
            "substitution": "echo $(python3 train.py)",
            "quoted": 'echo "$(python3 train.py)"',
            "other": "ls",
            "already_th": "th-job-start.sh ignored -- true",
        }
        for route, flag, mode in (
            ("claude", "", ""),
            ("codex_bypass", "--codex", "bypassPermissions"),
            ("codex_on_request", "--codex", "on-request"),
            ("agy_skip", "--agy", ""),
            ("agy_interactive", "--agy", ""),
            ("worker", "", ""),
        ):
            for form, command in commands.items():
                payload = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                    "tool_use_id": f"{route}-{form}",
                    "session_id": "check",
                    "cwd": str(work),
                    "permission_mode": mode,
                }
                if flag == "--agy":
                    payload = {
                        "hookEventName": "PreToolUse",
                        "conversationId": "check",
                        "cwd": str(work),
                        "toolCall": {
                            "name": "run_command",
                            "id": f"{route}-{form}",
                            "args": {"CommandLine": command},
                        },
                    }
                cell_env = env.copy()
                expected_exit = 7 if form == "simple" else 0
                cell_env["FAKE_EXIT"] = str(expected_exit)
                if route == "worker":
                    cell_env["TH_ROLE"] = "worker"
                proc = subprocess.run(
                    ["python3", str(hook)] + ([flag] if flag else []),
                    input=json.dumps(payload),
                    text=True,
                    capture_output=True,
                    cwd=work,
                    env=cell_env,
                    timeout=15,
                    check=False,
                )
                try:
                    result = json.loads(proc.stdout) if proc.stdout.strip() else {}
                    nested = result.get("hookSpecificOutput", {})
                    wrapped = nested.get("updatedInput", {}).get(
                        "command"
                    ) or result.get("overwrite", {}).get("CommandLine")
                    denial = (
                        result.get("decision") == "deny"
                        or nested.get("permissionDecision") == "deny"
                    )
                    expected = route not in (
                        "worker",
                        "codex_on_request",
                    ) and form not in ("other", "already_th")
                    if (
                        proc.returncode
                        or denial
                        or "systemMessage" in result
                        or bool(wrapped) != expected
                    ):
                        raise ValueError(
                            f"hook rc={proc.returncode} output={proc.stdout.strip()} stderr={proc.stderr.strip()}"
                        )
                    if wrapped:
                        before = set((watch / "claude-jobs").glob("*/exit_code"))
                        run = subprocess.run(
                            [
                                "bash",
                                "-c",
                                wrapped + ("\nwait" if form == "background" else ""),
                            ],
                            cwd=work,
                            env=cell_env,
                            capture_output=True,
                            text=True,
                            timeout=20,
                            check=False,
                        )
                        paths = (
                            set((watch / "claude-jobs").glob("*/exit_code")) - before
                        )
                        # Background task completion may outlive the subshell's wait in some shells.
                        deadline = time.monotonic() + 3
                        while (
                            not paths
                            and form == "background"
                            and time.monotonic() < deadline
                        ):
                            time.sleep(0.05)
                            paths = (
                                set((watch / "claude-jobs").glob("*/exit_code"))
                                - before
                            )
                        if (
                            len(paths) != 1
                            or next(iter(paths)).read_text().strip()
                            != str(expected_exit)
                            or run.returncode != expected_exit
                        ):
                            raise ValueError(
                                f"execution rc={run.returncode} exit_files={len(paths)} stderr={run.stderr.strip()}"
                            )
                        record(
                            "PASS",
                            f"{route}/{form}",
                            f"th 작업 생성(hook), exit_code={expected_exit}",
                        )
                    else:
                        record("PASS", f"{route}/{form}", "그대로(의도)")
                except (
                    ValueError,
                    json.JSONDecodeError,
                    subprocess.TimeoutExpired,
                ) as exc:
                    record("FAIL", f"{route}/{form}", str(exc))
        for tool, argv in (
            ("codex", ["exec", "test"]),
            ("agy", ["-p", "test"]),
            ("claude", ["-p", "test"]),
        ):
            before = set((watch / "shim-jobs").glob("*/exit_code"))
            try:
                proc = subprocess.run(
                    [str(SHIM / tool), *argv],
                    env=env,
                    cwd=work,
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                paths = set((watch / "shim-jobs").glob("*/exit_code")) - before
                if (
                    proc.returncode
                    or len(paths) != 1
                    or next(iter(paths)).read_text().strip() != "0"
                ):
                    raise ValueError(
                        f"rc={proc.returncode} exit_files={len(paths)} stderr={proc.stderr.strip()}"
                    )
                record("PASS", f"shim/{tool}", "th 작업 생성(PATH shim), exit_code=0")
            except (ValueError, subprocess.TimeoutExpired) as exc:
                record("FAIL", f"shim/{tool}", str(exc))
        if args.live:
            for name, command in (
                (
                    "codex_bypass",
                    [
                        "codex",
                        "--dangerously-bypass-approvals-and-sandbox",
                        "exec",
                        "Reply OK",
                    ],
                ),
                ("codex_on_request", ["codex", "-a", "on-request", "exec", "Reply OK"]),
                ("agy", ["agy", "--dangerously-skip-permissions", "-p", "Reply OK"]),
                ("claude", ["claude", "-p", "Reply OK"]),
            ):
                try:
                    live_env = env.copy()
                    live_env["PATH"] = os.environ.get("PATH", "/usr/bin:/bin")
                    proc = subprocess.run(
                        command, cwd=work, env=live_env, timeout=60, check=False
                    )
                    record(
                        "PASS" if proc.returncode == 0 else "FAIL",
                        f"live/{name}",
                        f"rc={proc.returncode}",
                    )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    record("FAIL", f"live/{name}", str(exc))
        print(f"합계 PASS={counts['PASS']} FAIL={counts['FAIL']} SKIP={counts['SKIP']}")
        return int(counts["FAIL"] > 0)


if __name__ == "__main__":
    raise SystemExit(main())
