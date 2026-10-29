"""Generate 500 synthetic registry entries, half already settled."""

import os
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
old = time.time() - 3 * 86400
for index in range(500):
    task = root / f"synthetic-{index:03}"
    if index < 250:
        task.mkdir(parents=True, exist_ok=True)
        for name in ("DONE", ".th_daemon.done", ".th_digest.done", "VERIFIED"):
            path = task / name
            path.touch()
            os.utime(path, (old, old))
        marker = task / ".th_daemon.settled"
        marker.touch()
        os.utime(marker, (old + 1, old + 1))
    print(task)
