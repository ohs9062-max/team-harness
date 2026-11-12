from __future__ import annotations

import os
from pathlib import Path
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
        args, env=env, input="input\n", capture_output=True, text=True, check=False
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
    for name in (".bashrc", ".profile"):
        assert (tmp_path / name).read_text().count("team-harness shim >>>") == 1
    remove_shim(ROOT, tmp_path)
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
    env = dict(os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH="")
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
    env = dict(os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH=str(tmp_path))
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
    env = dict(os.environ, PATH=f"{shim}:{bin_dir}", PYTHONPATH=str(tmp_path))
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
