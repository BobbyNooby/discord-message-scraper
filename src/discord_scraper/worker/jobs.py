"""The job queue: one atomic claim, one job per worker.

`claim_next` is a single ``UPDATE … WHERE status='queued' RETURNING``. Two
workers polling at the same moment is the case this shape exists for: a
read-then-write pair would let both read the same queued row and run the same
rebuild twice, writing the same tables from two writers (spec §10.3).
"""

from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Callable

from discord_scraper.config import Config
from discord_scraper.rollups.build import rebuild_all
from discord_scraper.worker.probe import probe_token
from discord_scraper.worker.state import record_worker_seen, set_worker_state, worker_state

JOB_FIELDS = ("id", "channel_id", "kind", "status", "requested_at", "started_at",
              "finished_at", "result", "error")

# A queued job an admin cleared. `claim_next` only ever takes `queued`, so a
# cancelled row is skipped by construction rather than by a second check.
CANCELLED = "cancelled"


class JobNotFound(LookupError):
    """No job with that id."""


class JobNotCancellable(RuntimeError):
    """The job's status is not one an admin may cancel."""


class JobRunning(JobNotCancellable):
    """A job already in flight — refusing is the honest answer."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def enqueue(conn: sqlite3.Connection, channel_id: str, kind: str) -> int:
    cursor = conn.execute(
        "INSERT INTO jobs (channel_id, kind, status, requested_at) VALUES (?, ?, 'queued', ?)",
        (str(channel_id), str(kind), _now()),
    )
    conn.commit()
    return int(cursor.lastrowid)


def cancel_job(conn: sqlite3.Connection, job_id: int) -> dict[str, Any]:
    """Clear a queued job, or explain why it cannot be cleared.

    Only ``queued`` is cancellable. A ``running`` rebuild is mid-write and
    killing it would leave the rollups half-built, so it is refused rather than
    pretended away. The ``UPDATE ... WHERE status='queued'`` is the real guard:
    a worker claiming the row between the read and the write wins, and the
    caller gets the same honest refusal.
    """
    job_id = int(job_id)
    row = conn.execute("SELECT kind, status FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise JobNotFound(f"no job with id {job_id}")
    kind, previous = str(row[0]), str(row[1])
    if previous == CANCELLED:
        return {"id": job_id, "kind": kind, "status": CANCELLED,
                "previous_status": CANCELLED, "cancelled_at": None, "already": True}
    if previous == "running":
        raise JobRunning(
            f"job {job_id} ({kind}) is running — a rebuild in flight cannot be "
            "interrupted safely; wait for it to finish"
        )
    if previous != "queued":
        raise JobNotCancellable(f"job {job_id} is already {previous} — nothing to cancel")
    now = _now()
    cursor = conn.execute(
        "UPDATE jobs SET status = ?, finished_at = ?, phase = ? "
        "WHERE id = ? AND status = 'queued'",
        (CANCELLED, now, CANCELLED, job_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        raise JobRunning(f"job {job_id} was claimed by the worker before it could be cancelled")
    return {"id": job_id, "kind": kind, "status": CANCELLED,
            "previous_status": previous, "cancelled_at": now, "already": False}


def claim_next(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """Claim the oldest queued job, or ``None`` if there is not one to claim."""
    found = conn.execute(
        """
        UPDATE jobs
           SET status = 'running', started_at = ?
         WHERE id = (SELECT id FROM jobs WHERE status = 'queued' ORDER BY id LIMIT 1)
           AND status = 'queued'
        RETURNING id, channel_id, kind, status, requested_at, started_at,
                  finished_at, result, error
        """,
        (_now(),),
    ).fetchone()
    conn.commit()
    return None if found is None else dict(zip(JOB_FIELDS, found))


def _ingest(conn: sqlite3.Connection, cfg: Config, kind: str) -> str:
    """Run the existing scrape path unchanged, then queue a rebuild (spec §10.5)."""
    from discord_scraper import overrides, pipeline
    from discord_scraper.rollups.build import record_event

    # The same resolution the API and the rebuild use. An overridden channel or
    # policy must change what is fetched, not only what the board reads; the
    # whole resolved config is handed down so no later setting drifts again.
    effective = overrides.effective_for(conn, cfg)
    job = pipeline.catch_up if kind == "catch_up" else pipeline.backfill
    started = time.monotonic()
    record_event(conn, source="worker", scope="ingest", phase="started",
                 message=f"{kind} started")
    try:
        added = job(conn, effective, pipeline.Budget(None), started)
    except Exception as exc:
        record_event(conn, source="worker", scope="ingest", level="error", phase="failed",
                     message=f"{kind} failed: {type(exc).__name__}: {exc}",
                     seconds=round(time.monotonic() - started, 3))
        raise
    seconds = round(time.monotonic() - started, 3)
    set_worker_state(conn, "last_ingest_at", _now())
    set_worker_state(conn, "last_ingest_added", added)
    enqueue(conn, effective.channel_id, "rebuild_rollups")
    # Counts and a duration, never a message: the same privacy line as the rest
    # of the dashboard.
    record_event(conn, source="worker", scope="ingest", phase="finished",
                 rows=int(added), seconds=seconds,
                 message=f"ingest: {added} new messages in {seconds:.1f}s")
    return f"{kind}: {added} messages added"


def set_job_phase(conn: sqlite3.Connection, job_id: int, phase: str) -> None:
    """Say what an in-flight job is doing, beside its ``running`` status."""
    conn.execute("UPDATE jobs SET phase = ? WHERE id = ?", (str(phase), int(job_id)))
    conn.commit()


def _rebuild(conn: sqlite3.Connection, cfg: Config,
             on_phase: Callable[[str], None] | None = None,
             holder: str = "worker rebuild") -> str:
    from contextlib import nullcontext

    from discord_scraper import locks, overrides

    # One rebuild at a time. A second one past the busy timeout would die with
    # `database is locked` and commit nothing, so it refuses up front and names
    # whoever holds the lock instead.
    db = locks.database_path(conn)
    lock = locks.hold(db, kind="rebuild", holder=holder) if db else nullcontext()
    with lock:
        # The same resolution the API uses, so a rebuild bakes the zone the
        # dashboard will compare against and the guard stays satisfiable.
        counts = rebuild_all(conn, overrides.effective_for(conn, cfg), on_phase=on_phase)
    return f"rebuilt {len(counts)} families"


def _probe(conn: sqlite3.Connection, cfg: Config) -> str:
    alive, status = probe_token(conn, cfg.token)
    return f"probe: HTTP {status}, token {'alive' if alive else 'not alive'}"


# Replaced wholesale in tests; the keys are the job kinds.
DISPATCH: dict[str, Callable[[sqlite3.Connection, Config], str]] = {
    "catch_up": lambda conn, cfg: _ingest(conn, cfg, "catch_up"),
    "backfill": lambda conn, cfg: _ingest(conn, cfg, "backfill"),
    "rebuild_rollups": _rebuild,
    "probe": _probe,
}


def run_job(
    conn: sqlite3.Connection,
    cfg: Config,
    job: dict[str, Any] | None,
    *,
    get: Callable[..., object] | None = None,
) -> None:
    """Run one claimed job, recording its outcome either way.

    A failure is written to the row and then re-raised: the worker loop decides
    whether to keep going, but the database always remembers what happened.
    """
    if job is None:
        return
    kind = str(job["kind"])
    handler = DISPATCH.get(kind)
    try:
        if handler is None:
            raise ValueError(f"unknown job kind: {kind}")
        if kind == "probe" and get is not None:
            alive, status = probe_token(conn, cfg.token, get=get)
            result = f"probe: HTTP {status}, token {'alive' if alive else 'not alive'}"
        elif kind == "rebuild_rollups":
            # A rebuild is the only job that runs for a minute, so it is the one
            # whose phase is worth persisting while it is in flight.
            result = _rebuild(
                conn, cfg,
                on_phase=lambda phase: set_job_phase(conn, job["id"], phase),
                holder=f"worker job #{job['id']}",
            )
        else:
            result = handler(conn, cfg)
    except Exception as exc:
        conn.execute(
            "UPDATE jobs SET status = 'failed', finished_at = ?, phase = 'failed', "
            "error = ? WHERE id = ?",
            (_now(), f"{type(exc).__name__}: {exc}", job["id"]),
        )
        conn.commit()
        raise
    conn.execute(
        "UPDATE jobs SET status = 'done', finished_at = ?, phase = 'finished', "
        "result = ? WHERE id = ?",
        (_now(), result, job["id"]),
    )
    conn.commit()
    record_worker_seen(conn)


def run_pending(conn: sqlite3.Connection, cfg: Config) -> int:
    """Drain the queue. Returns how many jobs ran."""
    ran = 0
    while True:
        job = claim_next(conn)
        if job is None:
            return ran
        run_job(conn, cfg, job)
        ran += 1
