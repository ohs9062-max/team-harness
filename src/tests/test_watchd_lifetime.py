import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest


@pytest.mark.parametrize("change", ["remove", "replace", "lock", "unchanged"])
def test_watchd_exits_when_watch_identity_changes(tmp_path: Path, change: str) -> None:
    watch = tmp_path / "watch"
    env = os.environ | {
        "HOME": str(tmp_path),
        "TH_WATCH_DIR": str(watch),
        "TH_POLL_SEC": "1",
        "TH_NO_MAINTENANCE": "1",
    }
    env.pop("TH_ONCE", None)
    script = Path(__file__).resolve().parents[2] / "scripts/th-watchd.sh"
    proc = subprocess.Popen(["bash", str(script)], env=env)
    try:
        deadline = time.monotonic() + 10
        children_file = Path(f"/proc/{proc.pid}/task/{proc.pid}/children")
        children: list[int] = []
        while time.monotonic() < deadline:
            if (watch / "watchd.lock").exists() and children_file.exists():
                children = [
                    int(pid)
                    for pid in children_file.read_text().split()
                    if (
                        Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
                    ).endswith(b"sleep")
                ]
                if children:
                    break
            time.sleep(0.02)
        assert children, "daemon did not enter its polling interval"
        if change == "lock":
            (watch / "watchd.lock").rename(watch / "old.lock")
            (watch / "watchd.lock").touch()
        elif change != "unchanged":
            shutil.rmtree(watch)
            if change == "replace":
                watch.mkdir()
        if change == "unchanged":
            time.sleep(3)
            assert proc.poll() is None
        else:
            assert proc.wait(timeout=4) == 0
            assert all(not Path(f"/proc/{pid}").exists() for pid in children)
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)
