"""Advisory file locks for the two "only one of these" situations.

SQLite serialises writers, but a rebuild holds its write transaction for
minutes. A second writer past the busy timeout dies with ``database is locked``
and, because a rebuild commits only at the end, leaves the rollups untouched —
which is how four concurrent writers put the live board into a state no rebuild
could clear.

Two locks, both taken with ``flock``:

- ``kind="rebuild"`` — one rebuild at a time, shared by the worker and the CLI.
- ``kind="worker"`` — one ingest worker per database.

``flock`` is held on a file descriptor and released by the kernel when that
descriptor closes, including on a crash, so a killed rebuild cannot leave a
stale lock. That is the whole reason this is a lock file next to the database
and not a control-plane row, which would need a heartbeat and a staleness
heuristic to be safe.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no flock
    fcntl = None


class Busy(RuntimeError):
    """Another process already holds the lock; the message names it."""


def _holder_text(fd: int) -> str:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        return os.read(fd, 256).decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _message(kind: str, holder: str) -> str:
    who = holder or "another process"
    if kind == "rebuild":
        return (f"a rebuild is already running ({who}) — "
                "wait for it to finish before rebuilding again")
    if kind == "worker":
        return (f"another ingest worker is already running ({who}) — "
                "stop it before starting a second one")
    return f"another {kind} is already running ({who})"


@contextmanager
def hold(db_path: str | Path, *, kind: str, holder: str) -> Iterator[None]:
    """Take ``kind``'s lock beside ``db_path``, or raise :class:`Busy`."""
    path = Path(f"{db_path}.{kind}.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if fcntl is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Busy(_message(kind, _holder_text(fd))) from None
        os.ftruncate(fd, 0)
        os.write(fd, f"{holder} (pid {os.getpid()})".encode("utf-8"))
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def database_path(conn: sqlite3.Connection) -> str | None:
    """The file behind a connection, or ``None`` for an in-memory database."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main":
            return str(row[2]) or None
    return None
