"""The tool owns its writable handle.

``connect_writable`` lives in ``discord_scraper.db``. The dashboard re-exports
this one object, but that re-export is pinned in the dashboard's own suite —
importing ``discord_stats`` from here would fail in a tool-only checkout.
"""

import sqlite3


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
