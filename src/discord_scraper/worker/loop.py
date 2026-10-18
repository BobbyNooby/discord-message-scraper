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
from discord_scraper.worker.state import record_worker_seen, set_worker_state, worker_state

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

    Start and stop are logged to the control plane so the live feed can say
    whether a worker is running at all — the process that can tell you is the
    one you are looking for.
    """
    from discord_scraper.rollups.build import record_event

    try:
        record_event(conn, source="worker", scope="worker", phase="started",
                     message="worker started")
    except sqlite3.Error:
        pass   # a liveness line must not stop the worker from starting
    passes = 0
    try:
        while iterations is None or passes < iterations:
            record_worker_seen(conn)
            if schedule_enabled(conn):
                last = worker_state(conn, "last_ingest_at")
                if _hours_since(last) >= next_delay_seconds(conn) / 3600:
                    enqueue(conn, cfg.channel_id, "catch_up")
            try:
                ran = run_pending(conn, cfg)
            except Exception as exc:
                # A job failure must not end ingest for the life of the process. A
                # dead token makes `fetch_messages` raise, and `run_pending`
                # re-raises by design; without this catch the daemon thread dies and
                # every later job — including a manual Recalculate — sits queued
                # until someone restarts the server (spec §10.1, §10.7).
                failures = int(worker_state(conn, "consecutive_failures") or 0) + 1
                set_worker_state(conn, "consecutive_failures", failures)
                set_worker_state(conn, "last_error", f"{type(exc).__name__}: {exc}")
            else:
                # Cleared by a job that *ran*, not by an idle pass: an empty queue
                # is not evidence that the token works again.
                if ran and (worker_state(conn, "consecutive_failures") or "0") != "0":
                    set_worker_state(conn, "consecutive_failures", 0)
            passes += 1
            if iterations is None or passes < iterations:
                sleep(poll_seconds)
        return passes
    finally:
        # SIGKILL leaves no stop line, which is honest: an abrupt death is
        # indistinguishable from a worker that is still running.
        try:
            record_event(conn, source="worker", scope="worker", phase="finished",
                         message=f"worker stopped after {passes} passes")
        except sqlite3.Error:
            pass
