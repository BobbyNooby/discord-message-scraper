"""The worker's main loop: drain the queue, ingest on schedule, then rest.

Kept separate from `jobs` so the queue and the dispatch can be tested without a
timing loop, and separate from `serve` so the loop can run in a thread beside
the API — the worker is the only writer, the API is the only reader.
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Callable

from discord_scraper.config import Config
from discord_scraper.worker.jobs import enqueue, run_pending
from discord_scraper.worker.scheduler import schedule_enabled, next_delay_seconds
from discord_scraper.worker.state import record_worker_seen, worker_state

POLL_SECONDS = 15


def _hours_since(stamp: str | None) -> float:
    if not stamp:
        return float("inf")
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds() / 3600


def run_forever(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    poll_seconds: int = POLL_SECONDS,
    iterations: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Run the queue forever (or ``iterations`` times, for tests).

    Each pass: heartbeat, enqueue a scheduled ingest when one is due, then run
    whatever is queued. The scheduled ingest is enqueued rather than run inline
    so it goes through exactly the same claim path as a manual one.
    """
    passes = 0
    while iterations is None or passes < iterations:
        record_worker_seen(conn)
        if schedule_enabled(conn):
            last = worker_state(conn, "last_ingest_at")
            if _hours_since(last) >= next_delay_seconds(conn) / 3600:
                enqueue(conn, cfg.channel_id, "catch_up")
        run_pending(conn, cfg)
        passes += 1
        if iterations is None or passes < iterations:
            sleep(poll_seconds)
    return passes
