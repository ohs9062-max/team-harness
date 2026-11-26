import os
from pathlib import Path


def test_default_daemon_disabled_and_home_is_isolated() -> None:
    assert os.environ["TH_NO_DAEMON"] == "1"
    assert Path(os.environ["HOME"]).name.startswith("th-test-home-")


def test_reaper_only_stops_owned_watchd(tmp_path: Path) -> None:
    import shutil
    import subprocess
    import sys
    import time

    import pytest

    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        pytest.skip("Linux pidfd required")
    root = Path(__file__).resolve().parents[2]
    fixture = tmp_path / "nested"
    fixture.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), fixture / "conftest.py")
    (fixture / "th-watchd.sh").write_text("#!/bin/bash\nwhile :; do sleep 1; done\n")
    (fixture / "test_nested.py").write_text(
        "import os, subprocess, time\n"
        "from pathlib import Path\n"
        "def test_spawn():\n"
        "    script = Path(__file__).with_name('th-watchd.sh')\n"
        "    p = subprocess.Popen(['bash', str(script)], stdout=subprocess.DEVNULL, "
        "stderr=subprocess.DEVNULL, start_new_session=True)\n"
        "    Path(__file__).with_name('pid').write_text(str(p.pid))\n"
        "    time.sleep(0.2)\n"
    )
    other = subprocess.Popen(["bash", str(fixture / "th-watchd.sh")])
    try:
        run = subprocess.run(
            [
                "bash",
                str(root / "scripts/th-test.sh"),
                str(fixture / "test_nested.py"),
                "-q",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert other.poll() is None
        pid = int((fixture / "pid").read_text())
        stat = Path(f"/proc/{pid}/stat")
        for _ in range(100):
            if not stat.exists() or stat.read_text().split(") ", 1)[1].startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("owned watchd survived pytest shutdown")
    finally:
        other.terminate()
        other.wait(timeout=5)
