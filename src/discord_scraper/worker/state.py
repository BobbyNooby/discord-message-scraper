"""Worker health, persisted so it survives a restart.

`worker_state` is a key/value table the API reads to render its banner and the
worker is the only writer (spec §5.2). Keeping it here — rather than in the
worker's memory — is what lets a dead token be visible after a restart, which is
the failure this table exists to prevent.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def set_worker_state(conn: sqlite3.Connection, key: str, value: object) -> None:
    conn.execute(
        """
        INSERT INTO worker_state (key, value, updated_at) VALUES (?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (str(key), str(value), _now()),
    )
    conn.commit()


def worker_state(conn: sqlite3.Connection, key: str) -> str | None:
    found = conn.execute("SELECT value FROM worker_state WHERE key = ?", (str(key),)).fetchone()
    return None if found is None else found[0]


def record_worker_seen(conn: sqlite3.Connection, now: str | None = None) -> None:
    """The heartbeat the admin board reads to tell "idle" from "died"."""
    set_worker_state(conn, "last_seen_at", now or _now())
