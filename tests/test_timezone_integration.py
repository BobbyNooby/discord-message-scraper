"""The timezone change, end to end: config → rebuild → different buckets.

The unit test in `test_timeutil.py` proves the conversion; this proves the whole
path uses it, including the day grain in `rollup_activity` and the hour in
`rollup_heat` (spec §7.2, §10.5).
"""

from discord_scraper.rollups.build import rebuild_all
from tests.conftest import author_raw, cfg_fixture, seed_messages


def _seed_a_late_message(conn):
    """23:30 UTC on a July date: midnight in London, 07:30 in Singapore."""
    seed_messages(conn, [(
        "tz1", "u1", "u1handle", "up late", "2026-07-15T23:30:00+00:00", None, None, 0, 0,
        author_raw("u1", "u1handle"),
    )])


def days_for(conn):
    return {row[0] for row in conn.execute("SELECT DISTINCT day FROM rollup_activity")}


def test_changing_timezone_moves_a_late_message_to_another_day(mem_db, tmp_path):
    _seed_a_late_message(mem_db)

    rebuild_all(mem_db, cfg_fixture(tmp_path, timezone="UTC"))
    assert "2026-07-15" in days_for(mem_db)

    rebuild_all(mem_db, cfg_fixture(tmp_path, timezone="Europe/London"))
    london = days_for(mem_db)
    assert "2026-07-16" in london          # BST, UTC+1: past local midnight
    assert "2026-07-15" not in london


def test_changing_timezone_moves_the_heat_hour(mem_db, tmp_path):
    _seed_a_late_message(mem_db)

    rebuild_all(mem_db, cfg_fixture(tmp_path, timezone="UTC"))
    hours = {row[0] for row in mem_db.execute("SELECT DISTINCT hour FROM rollup_heat")}
    assert 23 in hours

    rebuild_all(mem_db, cfg_fixture(tmp_path, timezone="Europe/London"))
    hours = {row[0] for row in mem_db.execute("SELECT DISTINCT hour FROM rollup_heat")}
    assert 0 in hours and 23 not in hours


def test_a_timezone_change_makes_the_rollups_stale(mem_db, tmp_path):
    from discord_scraper.rollups.build import config_digest
    from discord_scraper.rollups.schema import is_stale

    rebuilder = cfg_fixture(tmp_path, timezone="UTC")
    rebuild_all(mem_db, rebuilder)

    moved = cfg_fixture(tmp_path, timezone="Europe/London")
    # The digest now includes the database-held identity state, so it is computed
    # against the same connection the rebuild used.
    assert is_stale(mem_db, "c1", "activity", config_hash=config_digest(moved, mem_db)) is True
    assert is_stale(mem_db, "c1", "activity", config_hash=config_digest(rebuilder, mem_db)) is False
