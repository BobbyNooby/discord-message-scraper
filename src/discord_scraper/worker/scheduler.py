"""When the worker next runs.

Two keys, both operator-visible in the admin board: `cron_enabled` and
`cron_hours`. The schedule lives in `worker_state` rather than in the config
file so it can be toggled while the process runs — a cron you must restart to
disable is a cron that keeps running.
"""

from __future__ import annotations

import sqlite3

from discord_scraper.worker.state import worker_state

DEFAULT_HOURS = 6
MINIMUM_SECONDS = 60


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


def ingest_paused(conn: sqlite3.Connection) -> bool:
    """A token the probe called dead is not retried on a timer.

    The schedule resumes on its own once a probe or a manual ingest proves the
    token works again; until then a retry can only fail the same way.
    """
    return str(worker_state(conn, "token_status") or "").lower() == "dead"
