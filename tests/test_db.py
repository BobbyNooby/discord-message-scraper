"""The tool owns its database handles.

Both live in ``discord_scraper.db`` and the dashboard opens through them too;
that is pinned in the dashboard's own suite, since importing ``discord_stats``
from here would fail in a tool-only checkout.
"""

import sqlite3

import pytest

from discord_scraper.db import connect_readonly, connect_writable


def test_tool_writable_connection_sets_its_own_pragmas(tmp_path):
    conn = connect_writable(tmp_path / "tool.db")
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        timeout = int(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert mode == "wal"
        assert timeout >= 10_000
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


def test_both_handles_wait_for_a_lock_instead_of_failing(tmp_path):
    path = tmp_path / "messages.db"
    writer = connect_writable(path)
    reader = connect_readonly(path)
    try:
        for conn in (writer, reader):
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert writer.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        writer.close()
        reader.close()


def test_the_read_only_handle_cannot_write(tmp_path):
    path = tmp_path / "messages.db"
    connect_writable(path).close()
    conn = connect_readonly(path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope (x)")
    finally:
        conn.close()


def test_a_question_mark_in_the_folder_is_part_of_the_path(tmp_path):
    """An unquoted ``file:`` URI read everything after ``?`` as options."""
    folder = tmp_path / "odd?name#here"
    folder.mkdir()
    writer = connect_writable(folder / "messages.db")
    writer.execute("CREATE TABLE t (x)")
    writer.commit()
    writer.close()
    conn = connect_readonly(folder / "messages.db")
    try:
        assert conn.execute("SELECT name FROM sqlite_master").fetchone()["name"] == "t"
    finally:
        conn.close()
