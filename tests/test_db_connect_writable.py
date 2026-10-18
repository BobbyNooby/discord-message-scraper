"""The tool owns its writable handle; the dashboard only re-exports it.

After the split the tool must not reach into the dashboard for its own write
path, so ``connect_writable`` lives in ``discord_scraper.db`` and
``api.deps`` imports that one object rather than defining a second copy.
"""

import sqlite3


def test_api_deps_reexports_the_tool_function():
    from discord_scraper import db
    from discord_scraper.api import deps

    # Not an equal copy: the *same* object, so there is one implementation.
    assert deps.connect_writable is db.connect_writable


def test_tool_writable_connection_sets_its_own_pragmas(tmp_path):
    from discord_scraper.db import connect_writable

    conn = connect_writable(tmp_path / "tool.db")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert mode == "wal"
        assert timeout >= 10_000
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()
