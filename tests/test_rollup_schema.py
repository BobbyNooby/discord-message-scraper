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
    assert len(ROLLUP_TABLES) == len(FAMILIES) == 15


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


def test_an_older_database_gains_the_included_column():
    """The upgrade path: CREATE TABLE IF NOT EXISTS does not add columns.

    A running instance created before §5.6's flag failed its first write with
    "table author_names has no column named included"; every fixture creates
    fresh tables, so only a test that builds the *old* shape catches it.
    """
    import sqlite3

    from discord_scraper.rollups.schema import create_control_tables

    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(
            """
            CREATE TABLE author_names (
              channel_id TEXT NOT NULL,
              author_id  TEXT NOT NULL,
              name       TEXT NOT NULL,
              renamed    INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (channel_id, author_id)
            );
            """
        )
        conn.execute("INSERT INTO author_names VALUES ('c1', 'u1', 'Alice', 1)")
        conn.commit()

        create_control_tables(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(author_names)")}
        assert "included" in columns
        # an existing row is shown by default, and its name is not rewritten
        included, renamed, name = conn.execute(
            "SELECT included, renamed, name FROM author_names"
        ).fetchone()
        assert (included, renamed, name) == (1, 1, "Alice")
    finally:
        conn.close()


def test_the_cheap_staleness_check_uses_the_newest_id_only(mem_db):
    """What the dashboard asks on every load: ~0 ms, no scan.

    The thorough count comparison below it costs a 766 MB scan on the reference
    database, so the read path takes the indexed question — and the trade-off is
    pinned here: a differing count alone does NOT read as stale in cheap mode,
    which is why `rollups status` keeps the expensive check.
    """
    create_rollup_tables(mem_db)
    # Fixed-width ids, like Discord's snowflakes: lexicographic order is then
    # chronological, which is what the indexed check relies on.
    _insert_message(mem_db, "0009")
    set_provenance(mem_db, "c1", "activity", computed_at="now", source_max_id="0009",
                   source_count=999, timezone="UTC", config_hash="h")

    assert is_stale(mem_db, "c1", "activity", cheap=True) is False   # newest id matches
    assert is_stale(mem_db, "c1", "activity") is True                # count does not

    _insert_message(mem_db, "0010")
    assert is_stale(mem_db, "c1", "activity", cheap=True) is True    # a new message is caught


def test_a_fresh_control_plane_stamps_the_schema_version(mem_db):
    from discord_scraper.rollups.schema import SCHEMA_VERSION, SCHEMA_VERSION_KEY
    from discord_scraper.scraper.database import get_meta

    create_control_tables(mem_db)
    assert isinstance(SCHEMA_VERSION, int)
    assert SCHEMA_VERSION_KEY == "schema_version"
    assert get_meta(mem_db, SCHEMA_VERSION_KEY) == str(SCHEMA_VERSION)


def test_an_unstamped_database_reports_none(mem_db):
    from discord_scraper.rollups.schema import schema_version
    from discord_scraper.scraper.database import delete_meta

    create_control_tables(mem_db)
    delete_meta(mem_db, "schema_version")
    assert schema_version(mem_db) is None


def test_a_rebuild_refreshes_an_outdated_stamp(seeded_db_path, tmp_path):
    import sqlite3

    from discord_scraper.rollups.build import rebuild_all
    from discord_scraper.rollups.schema import SCHEMA_VERSION, schema_version
    from discord_scraper.scraper.database import set_meta
    from tests.conftest import cfg_fixture

    conn = sqlite3.connect(seeded_db_path)
    conn.row_factory = sqlite3.Row
    try:
        create_control_tables(conn)
        create_rollup_tables(conn)
        set_meta(conn, "schema_version", "0")   # built by an earlier revision
        rebuild_all(conn, cfg_fixture(tmp_path))
        assert schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_a_database_without_a_meta_table_reports_none_then_takes_the_stamp():
    """A pre-`meta` database is unstamped, not broken: reading is honest and a
    build can still stamp it."""
    import sqlite3

    from discord_scraper.rollups.schema import SCHEMA_VERSION, schema_version

    conn = sqlite3.connect(":memory:")
    try:
        assert schema_version(conn) is None
        create_control_tables(conn)
        assert schema_version(conn) == SCHEMA_VERSION
    finally:
        conn.close()


def test_migrating_the_control_plane_keeps_an_existing_stamp(mem_db):
    """A boot/migration must not relabel an old database as current: the whole
    point of the stamp is to notice exactly that database."""
    from discord_scraper.rollups.schema import schema_version
    from discord_scraper.scraper.database import set_meta

    create_control_tables(mem_db)
    set_meta(mem_db, "schema_version", "0")
    create_control_tables(mem_db)   # idempotent re-run, as the API boot does
    assert schema_version(mem_db) == 0
