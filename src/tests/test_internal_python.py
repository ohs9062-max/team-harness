"""Internal Python starts clean while worker tools retain their environment."""

import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]


def polluted(tmp_path: Path) -> dict[str, str]:
    poison = tmp_path / "poison"
    poison.mkdir()
    (poison / "sitecustomize.py").write_text("raise RuntimeError('contaminated')\n")
    return {
        **os.environ,
        "PYTHONPATH": str(poison),
        "PYTHONHOME": "/nonexistent",
        "VIRTUAL_ENV": "/nonexistent",
        "HOME": str(tmp_path),
        "TH_WATCH_DIR": str(tmp_path / "watch"),
        "TH_NO_MAINTENANCE": "1",
    }


def test_internal_python_cleans_environment(tmp_path: Path) -> None:
    env = polluted(tmp_path)
    result = subprocess.run(
        [
            str(ROOT / "scripts/th-env.sh"),
            "-c",
            "import os,team_harness; print(os.getenv('PYTHONPATH')); print(os.getenv('PYTHONHOME')); print(os.getenv('VIRTUAL_ENV')); print(os.getenv('PYTHONNOUSERSITE'))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines() == ["None", "None", "None", "1"]
    assert "contaminated" not in result.stderr


@pytest.mark.parametrize("tool", ["codex", "agy"])
def test_shim_preserves_cli_environment(tmp_path: Path, tool: str) -> None:
    env = polluted(tmp_path)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    cli = bindir / tool
    cli.write_text(
        '#!/bin/sh\nprintf "%s\\n%s\\n%s\\n" "$PYTHONPATH" "$PYTHONHOME" "$VIRTUAL_ENV"\n'
    )
    cli.chmod(0o755)
    env["PATH"] = f"{ROOT / 'shim'}:{bindir}:{env['PATH']}"
    result = subprocess.run(
        [str(ROOT / "shim" / tool)], env=env, text=True, capture_output=True, check=True
    )
    assert result.stdout.splitlines() == [
        env["PYTHONPATH"],
        "/nonexistent",
        "/nonexistent",
    ]
    assert "contaminated" not in result.stderr


def test_fallback_without_venv_or_uv(tmp_path: Path) -> None:
    env = polluted(tmp_path)
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src/team_harness").mkdir(parents=True)
    (root / "src/team_harness/__init__.py").touch()
    for name in ("th-env.sh", "th-python-bootstrap.py"):
        shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
    env["PATH"] = "/usr/bin:/bin"
    result = subprocess.run(
        [
            str(root / "scripts/th-env.sh"),
            "-c",
            "import team_harness,os; print(os.getenv('PYTHONPATH'))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "None"


def test_uv_fallback(tmp_path: Path) -> None:
    env = polluted(tmp_path)
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    (root / "src/team_harness").mkdir(parents=True)
    (root / "src/team_harness/__init__.py").touch()
    for name in ("th-env.sh", "th-python-bootstrap.py"):
        shutil.copy(ROOT / "scripts" / name, root / "scripts" / name)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    uv = bindir / "uv"
    uv.write_text('#!/bin/sh\nshift 4\nexec /usr/bin/python3 "$@"\n')
    uv.chmod(0o755)
    env["PATH"] = f"{bindir}:/usr/bin:/bin"
    result = subprocess.run(
        [
            str(root / "scripts/th-env.sh"),
            "-c",
            "import team_harness,os; print(os.getenv('PYTHONPATH'))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.strip() == "None"


def test_verify_receives_original_environment(tmp_path: Path) -> None:
    env = polluted(tmp_path)
    task = tmp_path / "task"
    task.mkdir()
    script = task / "verify.sh"
    script.write_text(
        'printf "%s|%s|%s|%s" "${PYTHONPATH-unset}" "$PYTHONHOME" "$VIRTUAL_ENV" "${PYTHONNOUSERSITE-unset}" > env-result\n'
    )
    result = subprocess.run(
        [
            str(ROOT / "scripts/th-env.sh"),
            "-c",
            "import sys; from pathlib import Path; from team_harness.worker_digest import check_script; check_script(Path(sys.argv[1]), force=True)",
            str(task),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.returncode == 0
    assert (task / "env-result").read_text() == "unset|/nonexistent|/nonexistent|unset"


def test_external_environment_restored(tmp_path: Path) -> None:
    env = polluted(tmp_path)
    result = subprocess.run(
        [
            str(ROOT / "scripts/th-env.sh"),
            "-c",
            "from team_harness.internal_python import caller_environment; e=caller_environment(); print(e['PYTHONPATH']); print(e['PYTHONHOME']); print(e['VIRTUAL_ENV']); print(e.get('PYTHONNOUSERSITE', 'unset'))",
        ],
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        env["PYTHONPATH"],
        "/nonexistent",
        "/nonexistent",
        "unset",
    ]
