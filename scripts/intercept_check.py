#!/usr/bin/env python3
"""Measure deployed execution adapters in an isolated temporary workspace."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

DEPLOYED = Path(__file__).resolve().parents[1]
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
            ("codex_on_request", "--codex", "default"),
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
                    reissue = route == "codex_on_request" and form not in (
                        "other",
                        "already_th",
                    )
                    if reissue:
                        reason = nested.get("permissionDecisionReason", "")
                        prefix = "This command must run through th. Reissue exactly this command: "
                        if not denial or not reason.startswith(prefix):
                            raise ValueError(
                                f"missing denial or wrapped command: {result}"
                            )
                        wrapped = reason[len(prefix) :]
                        again = dict(payload, tool_input={"command": wrapped})
                        replay = subprocess.run(
                            ["python3", str(hook), flag],
                            input=json.dumps(again),
                            text=True,
                            capture_output=True,
                            cwd=work,
                            env=cell_env,
                            timeout=15,
                            check=False,
                        )
                        if replay.returncode or replay.stdout.strip():
                            raise ValueError(
                                f"wrapped hook rc={replay.returncode} output={replay.stdout!r}"
                            )
                    expected = route != "worker" and form not in ("other", "already_th")
                    if (
                        proc.returncode
                        or (denial != reissue)
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
                            f"{'deny 재발행, ' if reissue else ''}th 작업 생성(hook), exit_code={expected_exit}",
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
        # Direct launcher and job runners do not themselves promise pipeline events.
        ledger = watch / "exec_pipeline.jsonl"
        launch_env = env | {
            "PATH": f"{fake}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "TH_WORKSPACE": str(work),
            "TH_WORKER_SUFFIX": str(base / "suffix.md"),
            "TH_CODEX_5H_BUDGET": "1000000",
            "TH_CODEX_SESSIONS_DIR": str(base / "sessions"),
            "TH_DIGEST_COMMAND": "/bin/true",
            "TH_AUTO_RECALL": "0",
            "TEAM_HARNESS_JEV": "0",
            "TH_BRIEF_OK": "isolated measurement",
        }
        (base / "suffix.md").write_text("fake worker")
        codex = fake / "codex"
        codex.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$@" > "$TH_WORKER_OUTPUT_DIR/args"\nprintf "done\\n" > "$TH_WORKER_OUTPUT_DIR/DONE"\n'
        )
        task = base / "launch-task"
        task.mkdir()
        (task / "task.md").write_text("---\nkind: code\ntask_key: matrix\n---\n")
        previous = ledger.read_bytes() if ledger.exists() else b""
        proc = subprocess.run(
            [str(DEPLOYED / "scripts/th-launch.sh"), str(task), "codex", str(work)],
            cwd=work,
            env=launch_env,
            text=True,
            capture_output=True,
            timeout=45,
            check=False,
        )
        if (
            proc.returncode == 0
            and (task / "DONE").exists()
            and (task / "args").exists()
        ):
            changed = ledger.exists() and ledger.read_bytes() != previous
            record(
                "PASS" if changed else "SKIP",
                "launch/codex",
                f"DONE 확인, 공통 사건 {'있음' if changed else '없음(빈칸)'}",
            )
        else:
            record(
                "FAIL",
                "launch/codex",
                f"실행 미완료 rc={proc.returncode} {proc.stderr[-300:].strip()}",
            )
        for wait in (False, True):
            job = base / ("job-wait" if wait else "job-direct")
            job.mkdir()
            previous = ledger.read_bytes() if ledger.exists() else b""
            code = 'printf "7\\n" > "$TH_JOB_TASK/exit_code"; exit 7'
            proc = subprocess.run(
                [
                    str(DEPLOYED / "scripts/th-job-start.sh"),
                    str(job),
                    *(["--wait"] if wait else []),
                    "--",
                    "bash",
                    "-c",
                    code,
                ],
                cwd=work,
                env=launch_env,
                text=True,
                capture_output=True,
                timeout=30,
                check=False,
            )
            # A detached job may finish after its launcher returns; wait for its result.
            deadline = time.monotonic() + 5
            while not (job / "exit_code").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            observed = (
                (job / "exit_code").read_text().strip()
                if (job / "exit_code").exists()
                else "missing"
            )
            changed = ledger.exists() and ledger.read_bytes() != previous
            ok = (
                observed == "7"
                and proc.returncode == (7 if wait else 0)
                and (job / "job.pid").exists()
            )
            record(
                "FAIL" if not ok else "PASS" if changed else "SKIP",
                f"job/{'wait' if wait else 'direct'}",
                f"rc={proc.returncode} exit_code={observed} 공통 사건 {'있음' if changed else '없음(빈칸)'}",
            )
        for cell, reason in (
            ("watchd/retry", "기존 테스트 및 코드 경로 참조; 실제 재시도 미실행"),
            ("watchd/continue", "기존 테스트 및 코드 경로 참조; 실제 자동 계속 미실행"),
            ("cli/run", "모델 세션 구동 미실측"),
            ("cli/repl", "대화형 진입점 미실측"),
            ("protocol/mode_c", "전체 역할 파이프라인 미실측"),
            ("sdk/run", "SDK 구동 미실측"),
        ):
            record("SKIP", cell, reason)
        repo, remote = base / "git-work", base / "git-remote.git"
        repo.mkdir()

        def git(*arguments: str, cwd: Path = repo) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["git", *arguments],
                cwd=cwd,
                env=launch_env,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

        git("init", "-q", "--bare", str(remote), cwd=base)
        git("init", "-q")
        git("config", "user.email", "matrix@example.invalid")
        git("config", "user.name", "Matrix")
        git("config", "core.hooksPath", str(DEPLOYED / ".githooks"))
        (repo / "base.txt").write_text("base\n")
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "base")
        branch = git("branch", "--show-current").stdout.strip()
        git("checkout", "-qb", "feature")
        (repo / "feature.txt").write_text("feature\n")
        git("add", ".")
        git("-c", "core.hooksPath=/dev/null", "commit", "-qm", "feature")
        git("checkout", "-q", branch)
        previous = ledger.read_bytes() if ledger.exists() else b""
        merge = git("merge", "--no-ff", "-m", "merge", "feature")
        changed = ledger.exists() and ledger.read_bytes() != previous
        record(
            "FAIL" if merge.returncode else "PASS" if changed else "SKIP",
            "git/merge",
            f"rc={merge.returncode} 공통 사건 {'있음' if changed else '없음(빈칸)'}",
        )
        previous = ledger.read_bytes() if ledger.exists() else b""
        push = git("push", str(remote), branch)
        changed = ledger.exists() and ledger.read_bytes() != previous
        record(
            "FAIL" if push.returncode else "PASS" if changed else "SKIP",
            "git/push",
            f"rc={push.returncode} 공통 사건 {'있음' if changed else '없음(빈칸)'}",
        )
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
