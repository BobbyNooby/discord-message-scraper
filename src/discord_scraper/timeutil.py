"""Local-time bucketing.

Buckets are derived in the configured zone, so a message near local midnight
lands on the day a reader would name it. A fixed offset gets that wrong for
half the year, which is why "auto" resolves to a real zone and not to
``datetime.now().astimezone().tzinfo`` (a fixed offset captured today).
"""

from __future__ import annotations

import os
from datetime import datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo


def resolve_tz(name: str) -> tzinfo:
    """Return the zone named by ``name``. ``"auto"`` means the host zone."""
    if name != "auto":
        return ZoneInfo(name)
    return _host_zone()


def _host_zone() -> tzinfo:
    """Best available DST-aware zone for this host, else a fixed offset."""
    env = os.environ.get("TZ")
    if env:
        try:
            return ZoneInfo(env)
        except Exception:
            pass

    etc_timezone = Path("/etc/timezone")  # Debian and friends
    if etc_timezone.is_file():
        try:
            return ZoneInfo(etc_timezone.read_text().strip())
        except Exception:
            pass

    localtime = Path("/etc/localtime")  # macOS and most Linux
    if localtime.is_symlink():
        target = str(localtime.resolve())
        marker = "zoneinfo/"
        if marker in target:
            try:
                return ZoneInfo(target.split(marker, 1)[1])
            except Exception:
                pass

    return datetime.now().astimezone().tzinfo


def local_parts(iso_utc: str, tz: str) -> tuple[str, int, int, int]:
    """Return ``(local_date, year, weekday, hour)`` for a UTC timestamp."""
    local = datetime.fromisoformat(iso_utc).astimezone(resolve_tz(tz))
    return local.date().isoformat(), local.year, local.weekday(), local.hour
