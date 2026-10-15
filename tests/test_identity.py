from discord_scraper.rollups.identity import Identities, latest_display_names, resolve_identities
from tests.conftest import cfg_fixture, seed_messages


def test_display_name_falls_back_when_global_name_is_absent(seeded_db):
    names = latest_display_names(seeded_db)
    assert names["u1"] == "GlobalName"      # present
    assert names["u2"] == "plain_user"      # global_name missing -> username
    assert names["u3"] == "u3"              # both missing -> id


def test_display_name_is_the_most_recent_not_the_loudest(seeded_db):
    """u2's *older* message carries a global_name; the newer one does not.

    MAX() over the column would answer "OldName" — the spec (§5.8) says the most
    recent message wins, so the answer is the username.
    """
    names = latest_display_names(seeded_db)
    assert names["u2"] == "plain_user"
    assert "OldName" not in names.values()


def test_bots_are_excluded_but_only_when_the_flag_is_true(seeded_db, cfg, tmp_path):
    keep = resolve_identities(seeded_db, cfg_fixture(tmp_path, exclude_bots=False))
    drop = resolve_identities(seeded_db, cfg_fixture(tmp_path, exclude_bots=True))
    assert "bot1" in keep.index
    assert "bot1" not in drop.index
    assert "u1" in drop.index          # a human has no bot key at all


def test_merged_alts_share_one_row(seeded_db, tmp_path):
    ids = resolve_identities(seeded_db, cfg_fixture(tmp_path, identities={"u1": ["u2"]}))
    assert ids.index["u1"] == ids.index["u2"]
    assert sorted(ids.accounts[ids.index["u1"]]) == ["u1", "u2"]
    assert len(ids.names) == 1          # one row: u3 is join-only, bot1 is a bot


def test_canonical_display_name_is_used_for_a_merged_person(seeded_db, tmp_path):
    ids = resolve_identities(seeded_db, cfg_fixture(tmp_path, identities={"u1": ["u2"]}))
    assert ids.names[ids.index["u1"]] == "GlobalName"


def test_groups_report_what_was_merged(seeded_db, tmp_path):
    ids = resolve_identities(seeded_db, cfg_fixture(tmp_path, identities={"u1": ["u2"]}))
    assert ids.groups == [("u1", ["u1", "u2"])]


def test_join_only_author_has_a_name_but_no_row(seeded_db, cfg):
    names = latest_display_names(seeded_db)
    ids = resolve_identities(seeded_db, cfg)
    assert "u3" in names
    assert "u3" not in ids.index


def test_identity_table_is_merged_with_the_config_map(seeded_db, tmp_path):
    """The admin page writes the `identity` table; config.toml is the file form."""
    from discord_scraper.rollups.schema import create_control_tables

    create_control_tables(seeded_db)
    seeded_db.execute(
        "INSERT INTO identity (channel_id, canonical_id, account_id, note) VALUES ('c1','u1','u2','alt')"
    )
    ids = resolve_identities(seeded_db, cfg_fixture(tmp_path))
    assert ids.index["u1"] == ids.index["u2"]


def test_authors_are_ordered_by_volume(seeded_db, cfg):
    ids = resolve_identities(seeded_db, cfg)
    assert ids.names[0] == "GlobalName"     # u1 has 3 counted messages, u2 has 2
    assert ids.names[1] == "plain_user"


def test_empty_database_yields_empty_identities(mem_db, cfg):
    ids = resolve_identities(mem_db, cfg)
    assert ids == Identities(index={}, names=[], accounts=[], groups=[], canonical={})
    assert latest_display_names(mem_db) == {}
