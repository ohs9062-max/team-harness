from __future__ import annotations

import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import time

import pytest

from team_harness.deployment import install_shim
from team_harness.deployment import remove_shim
from team_harness.deployment import shim_status

ROOT = Path(__file__).resolve().parents[2]


def setup_cli(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("codex", "agy"):
        fake = bin_dir / tool
        fake.write_text(
            '#!/bin/sh\nprintf "depth=%s args=%s\\n" "${TH_SHIM_DEPTH:-none}" "$*"\ncat\nexit 7\n'
        )
        fake.chmod(0o755)
    env = dict(
        os.environ,
        HOME=str(tmp_path),
        TH_WATCH_DIR=str(tmp_path / "watch"),
        PATH=f"{ROOT / 'shim'}:{bin_dir}:{os.environ['PATH']}",
    )
    return env, tmp_path / "watch" / "th.db"


def run(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        env=env,
        cwd=env["HOME"],
        input="input\n",
        capture_output=True,
        text=True,
        check=False,
    )


def test_passthrough_and_observation(tmp_path: Path) -> None:
    env, dbpath = setup_cli(tmp_path)
    assert run(env, "codex").stdout.startswith("depth=none")
    assert run(env, "codex", "resume").stdout.startswith("depth=none")
    assert run(dict(env, TH_SHIM="0"), "codex", "exec", "hello").stdout.startswith(
        "depth=none"
    )
    assert not dbpath.exists()
    proc = run(env, "codex", "exec", "-")
    assert proc.returncode == 7
    assert proc.stdout == "depth=1 args=exec -\ninput\n"
    with sqlite3.connect(dbpath) as db:
        assert db.execute("select depth,mode from shim_decisions").fetchall() == [
            (1, "observe")
        ]
    assert run(dict(env, TH_SHIM_DEPTH="2"), "agy", "-p", "hello").stdout.startswith(
        "depth=3"
    )


def test_failure_and_missing_binary(tmp_path: Path) -> None:
    env, _ = setup_cli(tmp_path)
    env["TH_WATCH_DIR"] = str(tmp_path / "bin" / "codex" / "no")
    assert run(env, "codex", "exec", "hello").returncode == 7
    env["TH_SHIM_MAX_DEPTH"] = "invalid"
    assert run(env, "agy", "-p", "hello").returncode == 7
    (tmp_path / "bin" / "codex").unlink()
    env["PATH"] = f"{ROOT / 'shim'}:{tmp_path / 'bin'}:/usr/bin:/bin"
    assert run(env, "codex", "exec", "hello").returncode == 127


def test_real_ancestor_bypass(tmp_path: Path) -> None:
    env, dbpath = setup_cli(tmp_path)
    launcher = tmp_path / "th-launch.sh"
    launcher.write_text("#!/bin/sh\ncodex exec hello\n")
    launcher.chmod(0o755)
    assert run(env, str(launcher)).stdout.startswith("depth=none")
    assert not dbpath.exists()


def test_install_remove(tmp_path: Path) -> None:
    for name in (".bashrc", ".profile"):
        (tmp_path / name).write_text("export EXISTING=1\n")
    install_shim(ROOT, tmp_path)
    install_shim(ROOT, tmp_path)
    assert shim_status(ROOT, tmp_path) == "installed"
    for tool in ("agy", "claude", "codex"):
        assert (
            tmp_path / ".team-harness/shim/bin" / tool
        ).resolve() == ROOT / "shim" / tool
    for name in (".bashrc", ".profile"):
        assert (tmp_path / name).read_text().count("team-harness shim >>>") == 1
    remove_shim(ROOT, tmp_path)
    assert not any((tmp_path / ".team-harness/shim/bin").iterdir())
    for name in (".bashrc", ".profile"):
        assert (tmp_path / name).read_text() == "export EXISTING=1\n"


def test_broken_or_slow_observer_keeps_real_command(tmp_path: Path) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_dir = tmp_path / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    for name in ("codex", "agy"):
        target = shim / name
        target.write_bytes((ROOT / "shim" / name).read_bytes())
        target.chmod(0o755)
        real = bin_dir / name
        real.write_text(
            '#!/bin/sh\nread -r line\nprintf "%s|%s\\n" "$*" "$line"\nexit 37\n'
        )
        real.chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH="", TH_EXEC_WRAP="0")
    python = python_dir / "python"
    for contents in (None, "#!/bin/sh\nexit 42\n", "#!/bin/sh\nexec /bin/sleep 5\n"):
        if contents is not None:
            python.write_text(contents)
            python.chmod(0o755)
        for name in ("codex", "agy"):
            started = time.monotonic()
            result = subprocess.run(
                [name, "exec", "two words"],
                env=env,
                input="payload\n",
                text=True,
                capture_output=True,
                check=False,
            )
            assert result.returncode == 37
            assert result.stdout == "exec two words|payload\n"
            assert result.stderr == ""
            assert time.monotonic() - started < 2


def test_observer_termination_and_inherited_stdout(tmp_path: Path) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    python_dir = tmp_path / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    (python_dir / "python").symlink_to("/usr/bin/python3")
    for name in ("codex", "agy"):
        target = shim / name
        target.write_bytes((ROOT / "shim" / name).read_bytes())
        target.chmod(0o755)
        real = bin_dir / name
        real.write_text(
            '#!/bin/sh\nread -r line\nprintf "%s|%s\\n" "$*" "$line"\nexit 37\n'
        )
        real.chmod(0o755)
    env = dict(
        os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH=str(tmp_path), TH_EXEC_WRAP="0"
    )
    scenarios = (
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10)\n",
        "import os, time\nif os.fork() == 0:\n    time.sleep(2)\n    os._exit(0)\nos._exit(0)\n",
    )
    for scenario in scenarios:
        (tmp_path / "sitecustomize.py").write_text(scenario)
        for name in ("codex", "agy"):
            started = time.monotonic()
            result = subprocess.run(
                [name, "exec", "two words"],
                env=env,
                input="payload\n",
                text=True,
                capture_output=True,
                check=False,
                timeout=1.5,
            )
            assert time.monotonic() - started < 1.5
            assert result.returncode == 37
            assert result.stdout == "exec two words|payload\n"
            assert result.stderr == ""


def test_status_displays_observed_block(tmp_path: Path) -> None:
    env, dbpath = setup_cli(tmp_path)
    run(dict(env, TH_SHIM_DEPTH="20"), "codex", "exec", "hello")
    assert dbpath.exists()
    result = subprocess.run(
        [str(ROOT / "scripts/th-status.sh")],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ℹ 실행 전 판정(관찰): 1건 차단 대상" in result.stdout


@pytest.mark.parametrize("name", ["codex", "agy"])
@pytest.mark.parametrize("variable", ["TH_SHIM_TIMEOUT", "TH_SHIM_KILL_AFTER"])
@pytest.mark.parametrize(
    "value", ["0", "0.0", "-1", "inf", "1d", "abc", "", "100", "0" * 100_000 + "1"]
)
def test_invalid_durations_keep_real_command(
    tmp_path: Path, name: str, variable: str, value: str
) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    target = shim / name
    target.write_bytes((ROOT / "shim" / name).read_bytes())
    target.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = bin_dir / name
    real.write_text(
        '#!/bin/sh\nread -r line\nprintf "%s|%s|%s\\n" "$1" "$2" "$line"\nexit 37\n'
    )
    real.chmod(0o755)
    python_dir = tmp_path / ".venv" / "bin"
    python_dir.mkdir(parents=True)
    (python_dir / "python").symlink_to("/usr/bin/python3")
    (tmp_path / "sitecustomize.py").write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(10)\n"
    )
    env = dict(
        os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH=str(tmp_path), TH_EXEC_WRAP="0"
    )
    env.pop("TH_SHIM_TIMEOUT", None)
    env.pop("TH_SHIM_KILL_AFTER", None)
    env[variable] = value
    started = time.monotonic()
    result = subprocess.run(
        [name, "exec", "two words"],
        env=env,
        input="payload\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=1.5,
    )
    assert time.monotonic() - started < 1.5
    assert result.returncode == 37
    assert result.stdout == "exec|two words|payload\n"
    assert result.stderr == ""


@pytest.mark.parametrize("name", ["codex", "agy"])
def test_fifo_result_does_not_block(tmp_path: Path, name: str) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    target = shim / name
    target.write_bytes((ROOT / "shim" / name).read_bytes())
    target.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = bin_dir / name
    real.write_text("#!/bin/sh\nexit 37\n")
    real.chmod(0o755)
    python = tmp_path / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(
        '#!/bin/sh\np=$(/usr/bin/readlink /proc/$$/fd/1)\n/bin/rm -f "$p"\n/usr/bin/mkfifo "$p"\n'
    )
    python.chmod(0o755)
    result = subprocess.run(
        [name, "exec", "hello"],
        env=dict(os.environ, PATH=f"{shim}:{bin_dir}"),
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert result.returncode == 37


@pytest.fixture
def clean_test_maintenance(tmp_path: Path):
    yield
    # The direct-execution test exercises real owner registration and therefore
    # starts maintenance. Only stop the process with this test's private HOME.
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            pidfd = os.pidfd_open(int(proc.name))
            try:
                args = (proc / "cmdline").read_bytes().split(b"\0")
                env = (proc / "environ").read_bytes().split(b"\0")
                if (
                    b"team_harness.maintenance" in args
                    and os.fsencode(f"HOME={tmp_path}") in env
                ):
                    signal.pidfd_send_signal(pidfd, signal.SIGTERM)
            finally:
                os.close(pidfd)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            pass


def test_active_direct_execution(tmp_path: Path, clean_test_maintenance: None) -> None:
    import json

    from team_harness import docs_migrate
    from team_harness import resource_slots

    env, dbpath = setup_cli(tmp_path)
    owner = tmp_path / "repo"
    owner.mkdir()
    (owner / ".git").mkdir()
    env["TH_WORKER_OUTPUT_DIR"] = str(owner)
    env.pop("TH_NO_MAINTENANCE", None)
    env.pop("TH_ONCE", None)
    prompt = "---\nallow_paths: /tmp\n---"
    proc = run(env, "codex", "exec", prompt)
    assert proc.returncode == 7
    assert proc.stdout.startswith("depth=1 args=exec ")
    assert "[th] warn allow_paths_ambiguous:1:" in proc.stderr
    rows = json.loads((tmp_path / "watch/admission.json").read_text())
    assert len(rows) == 1
    assert not resource_slots.active(next(iter(rows.values())))
    old = os.environ.get("TH_WATCH_DIR")
    os.environ["TH_WATCH_DIR"] = env["TH_WATCH_DIR"]
    try:
        assert owner in docs_migrate.owners()
    finally:
        if old is None:
            os.environ.pop("TH_WATCH_DIR")
        else:
            os.environ["TH_WATCH_DIR"] = old
    quiet_env = dict(
        env,
        TH_SHIM_QUIET="1",
        TH_WATCH_DIR=str(tmp_path / "quiet"),
        TH_NO_MAINTENANCE="1",
    )
    quiet = run(quiet_env, "codex", "exec", prompt)
    assert quiet.stderr == ""
    with sqlite3.connect(tmp_path / "quiet/th.db") as db:
        assert db.execute("select count(*) from shim_decisions").fetchone()[0] == 1


def test_active_slot_visible_during_execution(tmp_path: Path) -> None:
    from team_harness.resource_slots import current_workers

    env, _ = setup_cli(tmp_path)
    fake = tmp_path / "bin/codex"
    fake.write_text('#!/bin/sh\nprintf ready > "$HOME/ready"\nread line\n')
    proc = subprocess.Popen(
        ["codex", "exec", "hello"], env=env, cwd=tmp_path, stdin=subprocess.PIPE
    )
    try:
        for _ in range(200):
            if (tmp_path / "ready").exists():
                break
            time.sleep(0.01)
        assert (tmp_path / "ready").exists()
        assert current_workers(tmp_path, "codex", tmp_path / "watch")
    finally:
        assert proc.stdin is not None
        proc.stdin.write(b"done\n")
        proc.stdin.close()
        proc.wait(timeout=5)
    assert not current_workers(tmp_path, "codex", tmp_path / "watch")


def test_quota_and_depth_warn_without_blocking(tmp_path: Path) -> None:
    env, _ = setup_cli(tmp_path)
    watch = tmp_path / "watch"
    watch.mkdir()
    (watch / "quota_lock.codex").write_text("quota exhausted")
    proc = run(dict(env, TH_SHIM_MAX_DEPTH="0"), "codex", "exec", "hello")
    assert proc.returncode == 7
    assert "[th] codex: quota lock active" in proc.stderr
    assert "[th] 실행 깊이" in proc.stderr
    assert (watch / "admission.json").exists()


def test_direct_executor_artifacts_and_signals(tmp_path: Path) -> None:
    import json

    env, _ = setup_cli(tmp_path)
    env.pop("TH_ROLE", None)
    env.pop("TH_JOB_TASK", None)
    env["TH_NO_DAEMON"] = "1"
    fake = tmp_path / "bin/codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, sys, time\n"
        "if os.getenv('SIGNAL_TEST'):\n"
        " def stop(sig, frame):\n"
        "  print(f'signal={sig}', flush=True); sys.exit(128 + sig)\n"
        " signal.signal(signal.SIGINT, stop); signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGHUP, stop)\n"
        " print('ready', flush=True)\n"
        " while True: time.sleep(1)\n"
        "print(sys.stdin.read(), end='', flush=True)\n"
        "print('error', file=sys.stderr, flush=True)\n"
        "sys.exit(int(os.getenv('FAKE_RC', '0')))\n"
    )
    for code in (0, 3):
        result = run(dict(env, FAKE_RC=str(code)), "codex", "exec", "-")
        assert result.returncode == code and result.stdout == "input\n"
        assert "error" in result.stderr
        task = Path((tmp_path / "watch/tasks.txt").read_text().splitlines()[-1])
        assert (task / "exit_code").read_text().strip() == str(code)
        assert f"rc={code}" in (task / "STATUS").read_text()
        assert (task / ("DONE" if code == 0 else "FAILED")).exists()
        assert not (task / "logs/job.log").exists()
        assert "out=-" in (task / "STATUS").read_text()
        assert int((task / "job.pid").read_text()) > 0
        events = [
            json.loads(line)
            for line in (tmp_path / "watch/exec_pipeline.jsonl")
            .read_text()
            .splitlines()
        ]
        assert any(
            x["phase"] == "post"
            and x["event"]["task"] == str(task)
            and x["event"]["exit_code"] == code
            for x in events
        )
    for sig in (signal.SIGTERM, signal.SIGHUP):
        proc = subprocess.Popen(
            ["codex", "exec", "-"],
            env=dict(env, SIGNAL_TEST="1"),
            cwd=tmp_path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        assert proc.stdout.readline() == "ready\n"
        proc.send_signal(sig)
        stdout, _ = proc.communicate(timeout=10)
        assert f"signal={sig.value}" in stdout and proc.returncode == 128 + sig


@pytest.mark.parametrize("name", ["codex", "agy", "claude"])
@pytest.mark.parametrize(
    "failure", ["missing_bootstrap", "nonexecutable", "import_failure"]
)
def test_direct_executor_bootstrap_failure(
    tmp_path: Path, name: str, failure: str
) -> None:
    shim = tmp_path / "shim"
    shim.mkdir()
    target = shim / name
    target.write_bytes((ROOT / "shim" / name).read_bytes())
    target.chmod(0o755)
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    env_script = scripts / "th-env.sh"
    env_script.write_bytes((ROOT / "scripts/th-env.sh").read_bytes())
    env_script.chmod(0o644 if failure == "nonexecutable" else 0o755)
    if failure == "import_failure":
        (scripts / "th-python-bootstrap.py").write_text("raise ImportError('broken')\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    real = bin_dir / name
    real.write_text('#!/bin/sh\nprintf "REAL_EXECUTED\\n"\n/bin/cat\nexit 7\n')
    real.chmod(0o755)
    env = dict(os.environ, PATH=f"{shim}:{bin_dir}:/usr/bin:/bin", HOME=str(tmp_path))
    for key in ("TH_ROLE", "TH_SHIM", "TH_EXEC_WRAP"):
        env.pop(key, None)
    result = subprocess.run(
        [name, "exec"],
        input="payload\n",
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    assert result.returncode == 7
    assert result.stdout == "REAL_EXECUTED\npayload\n"
    assert result.stderr == "th execution fail-open: executor unavailable\n"


def test_direct_executor_tty_and_ctrl_c(tmp_path: Path) -> None:
    import pty
    import select

    env, _ = setup_cli(tmp_path)
    env.pop("TH_ROLE", None)
    env.pop("TH_JOB_TASK", None)
    env.update(TH_NO_DAEMON="1", TH_SHIM_QUIET="1")
    fake = tmp_path / "bin/codex"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import os, signal, sys, time\n"
        "for fd in range(3): print(f'fd{fd}_tty={os.isatty(fd)}', flush=True)\n"
        "fd = os.open('/dev/tty', os.O_RDONLY); os.close(fd)\n"
        "print('controlling_tty=yes', flush=True)\n"
        "if os.getenv('INT_TEST'):\n"
        " signal.signal(signal.SIGINT, signal.SIG_DFL)\n"
        " print('ready', flush=True)\n"
        " while True: time.sleep(1)\n"
        "sys.exit(7)\n"
    )
    for interrupt in (False, True):
        pid, fd = pty.fork()
        if pid == 0:
            os.execvpe(
                "codex",
                ["codex", "exec", "probe"],
                dict(env, INT_TEST="1" if interrupt else ""),
            )
        output = b""
        try:
            while True:
                readable, _, _ = select.select([fd], [], [], 10)
                assert readable, output
                try:
                    data = os.read(fd, 4096)
                except OSError:
                    break
                if not data:
                    break
                output += data
                if interrupt and b"ready" in output:
                    os.killpg(pid, signal.SIGINT)
                    interrupt = False
            _, status = os.waitpid(pid, 0)
        finally:
            os.close(fd)
        assert b"fd0_tty=True" in output and b"fd1_tty=True" in output
        assert b"fd2_tty=True" in output and b"controlling_tty=yes" in output
        expected = 130 if b"ready" in output else 7
        assert os.waitstatus_to_exitcode(status) == expected
        task = Path((tmp_path / "watch/tasks.txt").read_text().splitlines()[-1])
        assert (task / "exit_code").read_text().strip() == str(expected)
        assert (task / "FAILED").exists() and (task / "job.pid").exists()


def test_direct_child_signal_exit_code(tmp_path: Path) -> None:
    env, _ = setup_cli(tmp_path)
    env.pop("TH_ROLE", None)
    env.pop("TH_JOB_TASK", None)
    env["TH_NO_DAEMON"] = "1"
    (tmp_path / "bin/codex").write_text("#!/bin/sh\nkill -TERM $$\n")
    result = run(env, "codex", "exec", "-")
    assert result.returncode == 143
    task = Path((tmp_path / "watch/tasks.txt").read_text().splitlines()[-1])
    assert (task / "exit_code").read_text().strip() == "143"
    assert (task / "FAILED").exists()
