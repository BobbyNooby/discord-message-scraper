"""Low-level DB access: read-only connection, row helper, message filter."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from discord_scraper.db import connect_readonly, connect_writable
from discord_scraper.scraper.config import DB_PATH

REAL_MESSAGES = "type IN (0, 19)"  # excludes calls, joins, pin notices, ...


def connect(read_only: bool = True) -> sqlite3.Connection:
    if not Path(DB_PATH).exists():
        sys.exit(f"No database at {DB_PATH} — run `discord-scraper scrape` first.")
    return connect_readonly(DB_PATH) if read_only else connect_writable(DB_PATH)


def q(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()
