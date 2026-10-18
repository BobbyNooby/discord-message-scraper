"""The tool's own writable database handle.

The tool owns the schema, WAL, the busy timeout and the flock guards, so the
write path belongs here rather than in the dashboard. ``api.deps`` imports this
function rather than defining its own copy.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_writable(db_path: str | Path) -> sqlite3.Connection:
    """A read/write handle, for the control plane only.

    Message data is read through ``connect`` (read-only). Sessions, jobs and
    worker state are rows this process must write, so they need a second handle;
    keeping them separate is what preserves "the API cannot alter message data" —
    the guarantee is the file handle, not a coding convention.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
