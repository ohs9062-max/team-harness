"""Shared quota lock expiry calculation."""

from datetime import datetime
from datetime import timedelta
from pathlib import Path
import re
import time


def expiry_for_message(message: str, now: datetime | None = None) -> int:
    now = now or datetime.now()
    match = re.search(r"try again at\s+(\d{1,2}:\d{2}\s*[AP]M)", message, re.I)
    if match:
        clock = datetime.strptime(match.group(1).upper().replace(" ", ""), "%I:%M%p")
        reset = now.replace(
            hour=clock.hour, minute=clock.minute, second=0, microsecond=0
        )
        if reset < now:
            reset += timedelta(days=1)
        return int(reset.timestamp()) + 300
    seconds = (
        900 if re.search(r"at capacity|capacity exceeded", message, re.I) else 43200
    )
    return int(now.timestamp()) + seconds


def lock_expiry(path: Path, content: str) -> float:
    match = re.search(r"(?:^|\s)expires=(\d+)(?=\s|$)", content)
    if match:
        return float(match.group(1))
    match = re.match(r"(\d{4}-\d\d-\d\d \d\d:\d\d(?::\d\d)?)", content)
    if match:
        stamp = match.group(1)
        try:
            return (
                datetime.strptime(
                    stamp, "%Y-%m-%d %H:%M:%S" if len(stamp) == 19 else "%Y-%m-%d %H:%M"
                ).timestamp()
                + 43200
            )
        except ValueError:
            pass
    return path.stat().st_mtime + 43200


def active(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        expired = lock_expiry(path, content) <= time.time()
        if expired:
            try:
                marker = path.parent / ("." + path.name + ".expired")
                stat = path.stat()
                signature = f"{stat.st_mtime_ns}:{stat.st_size}"
                if not marker.exists() or marker.read_text() != signature:
                    with (path.parent / "events.log").open(
                        "a", encoding="utf-8"
                    ) as log:
                        log.write(
                            f"{datetime.now():%F %T} [quota expired] {path.name}\n"
                        )
                    marker.write_text(signature)
            except OSError:
                pass
        return not expired
    except FileNotFoundError:
        return False
    except OSError:
        return True
