"""A rebuild must bake the same configuration the dashboard compares against.

The API applies the control-plane operator overrides; if the rebuild path does
not, the two disagree forever and the guard can never be satisfied. These tests
pin the rebuild side of that contract for both the worker and the CLI.
"""

import sqlite3

from discord_scraper.rollups.schema import create_control_tables, create_rollup_tables
from discord_scraper.worker.jobs import enqueue, run_pending
from tests.conftest import cfg_fixture


def _control_tables(conn):
    create_rollup_tables(conn)
    create_control_tables(conn)
    conn.commit()
    return conn


def _set_override(conn, key, value):
    conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _zones_on_disk(path):
    conn = sqlite3.connect(path)
    try:
        return {str(row[0]) for row in conn.execute("SELECT DISTINCT timezone FROM rollup_meta")}
    finally:
        conn.close()


def test_the_worker_rebuild_records_the_override_zone(conn, seeded_db_path, tmp_path):
    cfg = cfg_fixture(tmp_path, timezone="UTC")
    _set_override(conn, "timezone", "Europe/London")
    enqueue(conn, "c1", "rebuild_rollups")

    run_pending(conn, cfg)

    assert _zones_on_disk(seeded_db_path) == {"Europe/London"}
    # The file itself is untouched: the override is the difference, not a rewrite.
    assert cfg.timezone == "UTC"


def test_the_cli_rebuild_records_the_override_zone(seeded_db_path, tmp_path):
    from discord_scraper.cli import main

    cfg_fixture(tmp_path, timezone="UTC")
    conn = _control_tables(sqlite3.connect(seeded_db_path))
    _set_override(conn, "timezone", "Europe/London")
    conn.close()

    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml")]) == 0
    assert _zones_on_disk(seeded_db_path) == {"Europe/London"}


def test_the_cli_rebuild_can_ignore_overrides(seeded_db_path, tmp_path):
    """The escape hatch: rebuild exactly what the file says, on purpose."""
    from discord_scraper.cli import main

    cfg_fixture(tmp_path, timezone="UTC")
    conn = _control_tables(sqlite3.connect(seeded_db_path))
    _set_override(conn, "timezone", "Europe/London")
    conn.close()

    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml"), "--ignore-overrides"]) == 0
    assert _zones_on_disk(seeded_db_path) == {"UTC"}
