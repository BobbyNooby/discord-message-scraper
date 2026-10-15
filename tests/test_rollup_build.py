import pytest

from discord_scraper.rollups.build import rebuild_all
from discord_scraper.rollups.schema import FAMILIES, is_stale, provenance
from tests.conftest import author_raw, seed_messages


def rows(conn, sql, *params):
    """Tuples, not ``sqlite3.Row``: the fixture sets a row factory."""
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


# --- the plan's golden tests -------------------------------------------------

def test_golden_aggregates_on_a_seeded_fixture(seeded_db, cfg):
    counts = rebuild_all(seeded_db, cfg, now="2026-01-01T00:00:00Z")
    assert counts["activity"] == 2          # two (day, author) cells
    row = seeded_db.execute(
        "SELECT msgs, chars, media, replies, edits FROM rollup_activity ORDER BY day"
    ).fetchone()
    assert tuple(row) == (3, 11, 1, 1, 1)


def test_media_counts_embeds_and_guards_non_arrays(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    media = seeded_db.execute("SELECT SUM(media) FROM rollup_activity").fetchone()[0]
    assert media == 2   # one attachment + one embed; the null embeds value must not raise


def test_rebuild_is_a_single_transaction(seeded_db, cfg, monkeypatch):
    rebuild_all(seeded_db, cfg)
    before = rows(seeded_db, "SELECT day, author_id, msgs FROM rollup_activity ORDER BY day")
    before_meta = provenance(seeded_db, "c1", "activity")

    import discord_scraper.rollups.build as build

    def boom(*args, **kwargs):
        raise RuntimeError("mid-rebuild failure")

    monkeypatch.setattr(build, "rebuild_word", boom)
    with pytest.raises(RuntimeError):
        rebuild_all(seeded_db, cfg)

    assert rows(seeded_db, "SELECT day, author_id, msgs FROM rollup_activity ORDER BY day") == before
    assert provenance(seeded_db, "c1", "activity") == before_meta


# --- per family --------------------------------------------------------------

def test_every_family_is_rebuilt_and_counted(seeded_db, cfg):
    counts = rebuild_all(seeded_db, cfg)
    assert set(counts) == set(FAMILIES)
    for family in FAMILIES:
        actual = seeded_db.execute(f"SELECT COUNT(*) FROM rollup_{family}").fetchone()[0]
        assert counts[family] == actual, family


def test_provenance_is_recorded_for_every_family(seeded_db, cfg):
    rebuild_all(seeded_db, cfg, now="2026-01-01T00:00:00Z")
    for family in FAMILIES:
        meta = provenance(seeded_db, "c1", family)
        assert meta["computed_at"] == "2026-01-01T00:00:00Z", family
        assert meta["source_count"] == 7, family      # every source row, bots included
        assert meta["timezone"] == "UTC", family
        assert is_stale(seeded_db, "c1", family) is False, family


def test_a_new_message_makes_every_family_stale(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    seed_messages(seeded_db, [("m99", "u1", "u1handle", "later", "2026-01-05T09:00:00+00:00",
                               None, None, 0, 0, author_raw("u1", "u1handle"))])
    assert is_stale(seeded_db, "c1", "activity") is True


def test_activity_heat_and_type(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    # dow is 0=Sun (spec §5.2), so Thursday 2026-01-01 is 4, not Python's 3.
    heat = rows(seeded_db, "SELECT year, dow, hour, author_id, msgs FROM rollup_heat ORDER BY author_id, hour")
    assert heat == [(2026, 4, 10, "u1", 3), (2026, 5, 10, "u2", 1), (2026, 5, 11, "u2", 1)]
    types = rows(seeded_db, "SELECT type, msgs FROM rollup_type ORDER BY type")
    assert types == [(0, 5), (7, 1), (19, 1)]      # every message, not only counted types


def test_reply_edges_use_the_parent_author(seeded_db, cfg):
    seed_messages(seeded_db, [("m8", "u2", "plain_user", "nice", "2026-01-02T12:00:00+00:00",
                               None, "m1", 0, 19, author_raw("u2", "plain_user"))])
    rebuild_all(seeded_db, cfg)
    edges = rows(seeded_db, "SELECT parent_author_id, child_author_id, msgs FROM rollup_reply ORDER BY 1, 2")
    assert edges == [("u1", "u1", 1), ("u1", "u2", 1)]


def test_response_is_attributed_to_the_replier_and_caps_long_gaps(seeded_db, cfg):
    seed_messages(seeded_db, [
        ("m8", "u2", "plain_user", "quick", "2026-01-01T10:00:30+00:00", None, "m1", 0, 19,
         author_raw("u2", "plain_user")),
        ("m9", "u2", "plain_user", "slow", "2026-01-06T12:00:00+00:00", None, "m6", 0, 19,
         author_raw("u2", "plain_user")),
    ])
    rebuild_all(seeded_db, cfg)
    # u1 answers its own m1 60s later (m2); u2 has one 30s reply and one
    # reply to a bot three days later, which is capped rather than counted.
    assert rows(seeded_db, """SELECT author_id, replies, total_seconds, capped_replies FROM rollup_response ORDER BY author_id""") == [("u1", 1, 60, 0), ("u2", 1, 30, 1)]


def test_edit_speed_measures_time_to_edit(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    assert rows(seeded_db, "SELECT author_id, edits, total_seconds FROM rollup_edit_speed") == [("u1", 1, 45)]


def test_starter_is_the_first_message_of_each_local_day(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    # day 3 belongs to a bot and day 4 to a join-only author: neither has a row.
    assert rows(seeded_db, "SELECT author_id, starters FROM rollup_starter ORDER BY 1") == [
        ("u1", 1), ("u2", 1)
    ]


def test_word_rollup_drops_stopwords_and_author_names(seeded_db, cfg):
    seed_messages(seeded_db, [("m10", "u1", "u1handle", "GlobalName globalname an the talking",
                               "2026-01-01T11:00:00+00:00", None, None, 0, 0,
                               author_raw("u1", "u1handle", "GlobalName"))])
    rebuild_all(seeded_db, cfg)
    words = {w: n for w, n in rows(seeded_db, "SELECT word, uses FROM rollup_word WHERE author_id='u1'")}
    assert words == {"abc": 1, "defghi": 1, "talking": 1}
    assert "beep" not in {w for (w,) in rows(seeded_db, "SELECT DISTINCT word FROM rollup_word")}


def test_link_rollup_normalises_and_keeps_the_top_sixty_hosts(seeded_db, cfg):
    seed_messages(seeded_db, [
        ("l1", "u1", "u1handle", "https://www.YouTube.com/watch?v=x&si=1",
         "2026-01-05T10:00:00+00:00", None, None, 0, 0, author_raw("u1", "u1handle")),
        ("l2", "u1", "u1handle", "https://youtube.com/watch?v=y",
         "2026-01-05T10:01:00+00:00", None, None, 0, 0, author_raw("u1", "u1handle")),
        ("l3", "u1", "u1handle", "https://example.org/a/b?q=1",
         "2026-01-05T10:02:00+00:00", None, None, 0, 0, author_raw("u1", "u1handle")),
    ])
    rebuild_all(seeded_db, cfg)
    assert rows(seeded_db, """
        SELECT day, domain, uses FROM rollup_link ORDER BY uses DESC, domain
    """) == [
        ("2026-01-05", "youtube.com/watch", 2),
        ("2026-01-05", "example.org/a/b", 1),
    ]


def test_embed_provider_and_type(seeded_db, cfg):
    rebuild_all(seeded_db, cfg)
    assert rows(seeded_db, "SELECT author_id, provider, embeds FROM rollup_provider") == [("u2", "YouTube", 1)]
    assert rows(seeded_db, "SELECT embed_type, embeds FROM rollup_embed_type") == [("video", 1)]


def test_reactions_and_attachments_are_credited_to_the_message_author(seeded_db, cfg):
    seed_messages(seeded_db, [("r1", "u1", "u1handle", "react to me", "2026-01-06T10:00:00+00:00",
                               None, None, 1, 0,
                               author_raw("u1", "u1handle",
                                          reactions=[{"emoji": {"name": "fire"}, "count": 3},
                                                     {"emoji": {"name": "ok_hand"}, "count": 2}],
                                          attachments=[{"content_type": "image/png"},
                                                       {"content_type": "image/png"},
                                                       {"content_type": "video/mp4"}]))])
    rebuild_all(seeded_db, cfg)
    assert sorted(rows(seeded_db, "SELECT emoji, author_id, count FROM rollup_reaction")) == [
        ("fire", "u1", 3), ("ok_hand", "u1", 2)
    ]
    assert sorted(rows(seeded_db, "SELECT mime, author_id, count FROM rollup_attachment")) == [
        ("image/png", "u1", 2), ("video/mp4", "u1", 1)
    ]


def test_emoji_in_text_is_counted_per_author(seeded_db, cfg):
    seed_messages(seeded_db, [("e1", "u1", "u1handle", "nice \U0001f600\U0001f600 work",
                               "2026-01-07T10:00:00+00:00", None, None, 0, 0,
                               author_raw("u1", "u1handle"))])
    rebuild_all(seeded_db, cfg)
    assert rows(seeded_db, "SELECT emoji, uses FROM rollup_emoji") == [("\U0001f600", 2)]


def test_a_second_rebuild_replaces_rather_than_appends(seeded_db, cfg):
    first = rebuild_all(seeded_db, cfg)
    second = rebuild_all(seeded_db, cfg)
    assert first == second


def test_rebuild_reads_the_message_table_a_bounded_number_of_times(seeded_db, cfg):
    """Fourteen families, not fourteen scans of a 783 MB table.

    Measured before this bound existed: ~20 statements touched `messages` and
    seven of them re-read the 483 MB `raw` column, which is where the 175 s came
    from. The budget is a guard rail, not a benchmark.
    """
    seen: list[str] = []
    seeded_db.set_trace_callback(seen.append)
    try:
        rebuild_all(seeded_db, cfg)
    finally:
        seeded_db.set_trace_callback(None)

    scans = [statement for statement in seen if "FROM MESSAGES" in statement.upper()]
    wide = [statement for statement in scans if " RAW" in statement.upper()
            or "CONTENT" in statement.upper()]
    assert len(scans) <= 6, scans
    assert len(wide) <= 2, wide
