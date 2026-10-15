import pytest

from discord_scraper.rollups.schema import (
    CONTROL_TABLES,
    FAMILIES,
    ROLLUP_TABLES,
    create_control_tables,
    create_rollup_tables,
    is_stale,
    provenance,
    set_provenance,
)


def _tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _pk_columns(conn, table):
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in sorted(rows, key=lambda r: r[5]) if r[5]]


def _insert_message(conn, mid, channel="c1", author="u1"):
    conn.execute(
        "INSERT INTO messages (id, channel_id, author_id, created_at, type) VALUES (?,?,?,?,0)",
        (mid, channel, author, "2026-01-01T00:00:00+00:00"),
    )


def test_provenance_roundtrip_and_staleness(mem_db):
    create_rollup_tables(mem_db)
    set_provenance(mem_db, "c1", "activity", computed_at="2026-01-01T00:00:00Z",
                   source_max_id="9", source_count=10, timezone="UTC", config_hash="h")
    p = provenance(mem_db, "c1", "activity")
    assert p["source_count"] == 10
    assert is_stale(mem_db, "c1", "activity") is True   # messages table is empty


def test_every_family_has_a_table_keyed_on_channel_id(mem_db):
    create_rollup_tables(mem_db)
    names = _tables(mem_db)
    for family in FAMILIES:
        assert f"rollup_{family}" in names, family
        assert _pk_columns(mem_db, f"rollup_{family}")[0] == "channel_id", family
    assert _pk_columns(mem_db, "rollup_meta") == ["channel_id", "family"]
    assert len(ROLLUP_TABLES) == len(FAMILIES) == 14


def test_create_is_idempotent(mem_db):
    create_rollup_tables(mem_db)
    create_rollup_tables(mem_db)   # IF NOT EXISTS, not a half-created schema
    assert {"rollup_activity", "rollup_meta"} <= _tables(mem_db)


def test_control_plane_tables_exist_and_are_separate_from_rollups(mem_db):
    create_control_tables(mem_db)
    names = _tables(mem_db)
    assert set(CONTROL_TABLES) <= names
    assert "rollup_activity" not in names   # the two calls do not overlap


def test_missing_provenance_is_stale(mem_db):
    create_rollup_tables(mem_db)
    assert provenance(mem_db, "c1", "activity") is None
    assert is_stale(mem_db, "c1", "activity") is True


def test_matching_source_count_and_max_id_are_not_stale(mem_db):
    create_rollup_tables(mem_db)
    _insert_message(mem_db, "9")
    set_provenance(mem_db, "c1", "activity", computed_at="now", source_max_id="9",
                   source_count=1, timezone="UTC", config_hash="h")
    assert is_stale(mem_db, "c1", "activity") is False


def test_a_new_message_makes_it_stale(mem_db):
    create_rollup_tables(mem_db)
    _insert_message(mem_db, "9")
    set_provenance(mem_db, "c1", "activity", computed_at="now", source_max_id="9",
                   source_count=1, timezone="UTC", config_hash="h")
    _insert_message(mem_db, "10")
    assert is_stale(mem_db, "c1", "activity") is True


def test_another_channels_messages_do_not_affect_staleness(mem_db):
    create_rollup_tables(mem_db)
    _insert_message(mem_db, "9")
    set_provenance(mem_db, "c1", "activity", computed_at="now", source_max_id="9",
                   source_count=1, timezone="UTC", config_hash="h")
    _insert_message(mem_db, "10", channel="c2")
    assert is_stale(mem_db, "c1", "activity") is False


def test_set_provenance_replaces_the_previous_row(mem_db):
    create_rollup_tables(mem_db)
    for count in (1, 2):
        set_provenance(mem_db, "c1", "heat", computed_at=f"t{count}", source_max_id="9",
                       source_count=count, timezone="UTC", config_hash="h")
    rows = mem_db.execute("SELECT COUNT(*) FROM rollup_meta WHERE channel_id='c1'").fetchone()[0]
    assert rows == 1
    assert provenance(mem_db, "c1", "heat")["source_count"] == 2


def test_provenance_works_without_a_row_factory():
    """A caller may hand us a plain connection; results must still be a mapping."""
    import sqlite3

    from discord_scraper.scraper.database import SCHEMA

    conn = sqlite3.connect(":memory:")   # no row_factory on purpose
    try:
        conn.executescript(SCHEMA)
        create_rollup_tables(conn)
        set_provenance(conn, "c1", "word", computed_at="now", source_max_id="1",
                       source_count=0, timezone="UTC", config_hash="h")
        assert provenance(conn, "c1", "word")["family"] == "word"
    finally:
        conn.close()


@pytest.mark.parametrize("family", FAMILIES)
def test_family_names_are_the_rollup_suffixes(mem_db, family):
    create_rollup_tables(mem_db)
    assert f"rollup_{family}" in _tables(mem_db)


def test_staleness_can_consider_the_config_digest(mem_db):
    create_rollup_tables(mem_db)
    _insert_message(mem_db, "9")
    set_provenance(mem_db, "c1", "word", computed_at="now", source_max_id="9",
                   source_count=1, timezone="UTC", config_hash="h")
    assert is_stale(mem_db, "c1", "word", config_hash="h") is False
    assert is_stale(mem_db, "c1", "word", config_hash="h2") is True
