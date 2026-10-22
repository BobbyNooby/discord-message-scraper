"""When the worker next runs.

Two keys, both operator-visible in the admin board: `cron_enabled` and
`cron_hours`. The schedule lives in `worker_state` rather than in the config
file so it can be toggled while the process runs — a cron you must restart to
disable is a cron that keeps running.
"""

from __future__ import annotations

import random
import sqlite3

from discord_scraper.config import Config
from discord_scraper.worker.state import worker_state

DEFAULT_HOURS = 6
MINIMUM_SECONDS = 60
# Repeated failures stretch the wait, up to a day (or the schedule itself, if
# that is longer). A failing token retried on the dot is the pattern most likely
# to get a self-botting account looked at.
MAX_BACKOFF_SECONDS = 24 * 3600
# Up to a tenth of the delay, so runs do not land on the same minute every day.
JITTER_FRACTION = 0.1


BACKUP_EVERY_HOURS = 24


def _switch(conn: sqlite3.Connection, key: str, default: bool = True) -> bool:
    """The stored value when there is one, otherwise ``default``."""
    stored = worker_state(conn, key)
    if stored is None:
        return default
    return str(stored).strip().lower() not in {"0", "false", "no", "off"}


# `cfg` is the resolved config (file plus overrides). A value saved from the
# admin board lands in `worker_state` and wins; without one, `[worker]` in
# config.toml decides. Reading only `worker_state` ignored the file entirely,
# so `cron_enabled = false` there did not stop the schedule.

def schedule_enabled(conn: sqlite3.Connection, cfg: Config | None = None) -> bool:
    return _switch(conn, "cron_enabled", cfg.cron_enabled if cfg is not None else True)


def backup_enabled(conn: sqlite3.Connection) -> bool:
    return _switch(conn, "backup_enabled")


def cron_hours(conn: sqlite3.Connection, cfg: Config | None = None) -> int:
    fallback = cfg.cron_hours if cfg is not None else DEFAULT_HOURS
    stored = worker_state(conn, "cron_hours")
    try:
        hours = int(str(stored))
    except (TypeError, ValueError):
        return fallback
    return hours if hours > 0 else fallback


def next_delay_seconds(conn: sqlite3.Connection, cfg: Config | None = None) -> int:
    """Seconds until the next scheduled ingest.

    Never zero: a delay of zero is a busy loop wearing a schedule's clothes.
    """
    return max(MINIMUM_SECONDS, cron_hours(conn, cfg) * 3600)


def should_run(conn: sqlite3.Connection, seconds_since_last: float) -> bool:
    return schedule_enabled(conn) and seconds_since_last >= next_delay_seconds(conn)


def delay_after_failures(conn: sqlite3.Connection, failures: int,
                         cfg: Config | None = None) -> int:
    """The scheduled delay, doubled for each consecutive failure after the first.

    One failure is usually the network, so it waits the normal delay; a run of
    them is usually the token, and waits longer each time.
    """
    base = next_delay_seconds(conn, cfg)
    if failures <= 1:
        return base
    return min(base * 2 ** (failures - 1), max(base, MAX_BACKOFF_SECONDS))


def jitter_seconds(conn: sqlite3.Connection, pick=random.randint, *,
                   cfg: Config | None = None) -> int:
    return pick(0, int(next_delay_seconds(conn, cfg) * JITTER_FRACTION))


def ingest_paused(conn: sqlite3.Connection) -> bool:
    """A token the probe called dead is not retried on a timer.

    The schedule resumes on its own once a probe or a manual ingest proves the
    token works again; until then a retry can only fail the same way.
    """
    return str(worker_state(conn, "token_status") or "").lower() == "dead"
