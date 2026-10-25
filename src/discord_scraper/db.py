"""The tool's database handles: one way to open for writing, one for reading.

The tool owns the schema, WAL, the busy timeout and the flock guards, so the
handles belong here rather than in the dashboard. Every caller in both repos
opens through these two, which is what keeps their settings from drifting:
four hand-written copies had already disagreed about the busy timeout.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def connect_writable(db_path: str | Path) -> sqlite3.Connection:
    """A read/write handle: the scraper, the worker, and the API's control plane.

    Message data is read through ``connect_readonly``. Sessions, jobs and
    worker state are rows the API must write, so they need a second handle;
    keeping them separate is what preserves "the API cannot alter message data" —
    the guarantee is the file handle, not a coding convention.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # WAL is a persistent, database-level setting, not a per-connection one:
    # one writer + many readers, and a reader is never blocked by an in-flight
    # commit. In the default 'delete' mode a committing writer blocks readers
    # outright, so any concurrent read fails with "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    # Wait for the write lock instead of failing immediately.
    conn.execute("PRAGMA busy_timeout=10000")
    # Durable under WAL (committed transactions survive a crash) at a fraction
    # of the fsync cost of the default FULL.
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def connect_readonly(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """A handle SQLite itself refuses to write through (``mode=ro``).

    The path goes in as a proper ``file:`` URI, so a ``?`` or ``#`` in a folder
    name is part of the path rather than the start of a query string.
    ``check_same_thread=False`` is for a caller that hands the handle to a
    worker thread and serialises its use.
    """
    uri = f"{Path(db_path).resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    # Readers wait out a checkpoint rather than failing outright.
    conn.execute("PRAGMA busy_timeout=10000")
    return conn
