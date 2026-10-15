import sqlite3

import pytest

from discord_scraper.cli import main
from discord_scraper.rollups.schema import FAMILIES
from tests.conftest import cfg_fixture


def test_status_reports_every_family(seeded_db_path, capsys):
    assert main(["rollups", "status", "--db", str(seeded_db_path)]) == 0
    out = capsys.readouterr().out
    for family in FAMILIES:
        assert family in out


def test_status_is_stale_before_a_rebuild_and_current_after(seeded_db_path, tmp_path, capsys):
    config = cfg_fixture(tmp_path)
    assert main(["rollups", "status", "--db", str(seeded_db_path)]) == 0
    assert "stale" in capsys.readouterr().out

    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml")]) == 0
    capsys.readouterr()
    assert main(["rollups", "status", "--db", str(seeded_db_path)]) == 0
    out = capsys.readouterr().out
    assert "stale" not in out
    assert out.count("current") == len(FAMILIES)
    assert config.channel_id == "c1"


def test_rebuild_writes_rows_and_reports_counts(seeded_db_path, tmp_path, capsys):
    cfg_fixture(tmp_path)
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml")]) == 0
    out = capsys.readouterr().out
    assert "activity" in out

    conn = sqlite3.connect(seeded_db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM rollup_activity").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM rollup_meta").fetchone()[0] == len(FAMILIES)
    finally:
        conn.close()


def test_rebuild_accepts_an_explicit_channel(seeded_db_path, tmp_path, capsys):
    cfg_fixture(tmp_path)
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path), "--channel", "c1",
                 "--config", str(tmp_path / "config.toml")]) == 0
    assert "activity" in capsys.readouterr().out


def test_rebuild_without_a_configuration_explains_itself(seeded_db_path, tmp_path, capsys):
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "absent.toml")]) != 0
    assert "config.toml" in capsys.readouterr().err


def test_a_missing_database_exits_non_zero(tmp_path, capsys):
    assert main(["rollups", "status", "--db", str(tmp_path / "absent.db")]) != 0
    assert "no database" in capsys.readouterr().err.lower()


def test_status_survives_a_database_with_no_messages(mem_db, tmp_path, capsys):
    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    try:
        from discord_scraper.scraper.database import SCHEMA

        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    assert main(["rollups", "status", "--db", str(path)]) == 0
    assert "no messages" in capsys.readouterr().out.lower()


def test_status_notices_a_word_list_change(seeded_db_path, tmp_path, capsys):
    """A stopword edit changes rollup_word without changing a source row."""
    cfg_fixture(tmp_path, ignore=["aaa"])
    config = tmp_path / "config.toml"
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(config)]) == 0
    capsys.readouterr()
    assert main(["rollups", "status", "--db", str(seeded_db_path),
                 "--config", str(config)]) == 0
    assert "stale" not in capsys.readouterr().out

    cfg_fixture(tmp_path, ignore=["aaa", "bbb"])
    assert main(["rollups", "status", "--db", str(seeded_db_path),
                 "--config", str(config)]) == 0
    assert "stale" in capsys.readouterr().out


def test_status_refuses_to_guess_between_channels(seeded_db_path, capsys):
    conn = sqlite3.connect(seeded_db_path)
    try:
        conn.execute(
            "INSERT INTO messages (id, channel_id, author_id, created_at, type) "
            "VALUES ('x2', 'c2', 'u1', '2026-01-01T00:00:00+00:00', 0)"
        )
        conn.commit()
    finally:
        conn.close()
    assert main(["rollups", "status", "--db", str(seeded_db_path)]) == 0
    assert "pass --channel" in capsys.readouterr().out


def test_a_channel_flag_selects_the_channel(seeded_db_path, capsys):
    assert main(["rollups", "status", "--db", str(seeded_db_path), "--channel", "other"]) == 0
    assert "no messages" not in capsys.readouterr().out


def test_serve_refuses_a_missing_database_without_binding_a_port(tmp_path, capsys):
    assert main(["serve", "--db", str(tmp_path / "absent.db")]) == 2
    assert "no database" in capsys.readouterr().err.lower()


def test_serve_accepts_host_and_port(tmp_path, capsys):
    from discord_scraper.cli import build_parser

    parsed = build_parser().parse_args(
        ["serve", "--db", str(tmp_path / "x.db"), "--host", "0.0.0.0", "--port", "9001"]
    )
    assert (parsed.host, parsed.port) == ("0.0.0.0", 9001)


def test_serve_has_three_shapes(tmp_path):
    from discord_scraper.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["serve", "--db", "x.db"]).api is False
    assert parser.parse_args(["serve", "--db", "x.db", "--api"]).api is True
    assert parser.parse_args(["serve", "--db", "x.db", "--worker"]).worker is True
    with pytest.raises(SystemExit):
        parser.parse_args(["serve", "--db", "x.db", "--api", "--worker"])


def test_an_unbuilt_spa_is_a_hint_not_a_crash(tmp_path, monkeypatch, capsys):
    from discord_scraper.api.deps import connect_writable
    from discord_scraper.cli import main
    from discord_scraper.rollups.schema import create_control_tables

    database = tmp_path / "messages.db"
    conn = connect_writable(database)
    create_control_tables(conn)
    conn.close()
    from tests.conftest import cfg_fixture

    cfg_fixture(tmp_path)
    seen = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: seen.update(app=app, **kw))
    # An explicit empty directory: the repo's own web/dist exists once the SPA is
    # built, so relying on it being absent would make this test depend on build state.
    assert main(["serve", "--db", str(database), "--config", str(tmp_path / "config.toml"),
                 "--api", "--port", "8099", "--static-dir", str(tmp_path / "no-such-dist")]) == 0
    assert "no built SPA" in capsys.readouterr().err
    assert seen["port"] == 8099
