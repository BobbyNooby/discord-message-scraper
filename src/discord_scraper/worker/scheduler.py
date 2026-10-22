"""When the worker next runs.

Two keys, both operator-visible in the admin board: `cron_enabled` and
`cron_hours`. The schedule lives in `worker_state` rather than in the config
file so it can be toggled while the process runs — a cron you must restart to
disable is a cron that keeps running.
"""

from __future__ import annotations

import random
import sqlite3

from discord_scraper.worker.state import worker_state

DEFAULT_HOURS = 6
MINIMUM_SECONDS = 60
# Repeated failures stretch the wait, up to a day (or the schedule itself, if
# that is longer). A failing token retried on the dot is the pattern most likely
# to get a self-botting account looked at.
MAX_BACKOFF_SECONDS = 24 * 3600
# Up to a tenth of the delay, so runs do not land on the same minute every day.
JITTER_FRACTION = 0.1


def schedule_enabled(conn: sqlite3.Connection) -> bool:
    stored = worker_state(conn, "cron_enabled")
    if stored is None:
        return True
    return str(stored).strip().lower() not in {"0", "false", "no", "off"}


def cron_hours(conn: sqlite3.Connection) -> int:
    stored = worker_state(conn, "cron_hours")
    try:
        hours = int(str(stored))
    except (TypeError, ValueError):
        return DEFAULT_HOURS
    return hours if hours > 0 else DEFAULT_HOURS


def next_delay_seconds(conn: sqlite3.Connection) -> int:
    """Seconds until the next scheduled ingest.

    Never zero: a delay of zero is a busy loop wearing a schedule's clothes.
    """
    return max(MINIMUM_SECONDS, cron_hours(conn) * 3600)


def should_run(conn: sqlite3.Connection, seconds_since_last: float) -> bool:
    return schedule_enabled(conn) and seconds_since_last >= next_delay_seconds(conn)


def delay_after_failures(conn: sqlite3.Connection, failures: int) -> int:
    """The scheduled delay, doubled for each consecutive failure after the first.

    One failure is usually the network, so it waits the normal delay; a run of
    them is usually the token, and waits longer each time.
    """
    base = next_delay_seconds(conn)
    if failures <= 1:
        return base
    return min(base * 2 ** (failures - 1), max(base, MAX_BACKOFF_SECONDS))


def jitter_seconds(conn: sqlite3.Connection, pick=random.randint) -> int:
    return pick(0, int(next_delay_seconds(conn) * JITTER_FRACTION))


def ingest_paused(conn: sqlite3.Connection) -> bool:
    """A token the probe called dead is not retried on a timer.

    The schedule resumes on its own once a probe or a manual ingest proves the
    token works again; until then a retry can only fail the same way.
    """
    return str(worker_state(conn, "token_status") or "").lower() == "dead"
