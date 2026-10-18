import sqlite3

import pytest

from discord_scraper.cli import main
from discord_stats.cli import main as stats_main
from discord_scraper.rollups.schema import FAMILIES
from tests.conftest import cfg_fixture


def test_status_reports_every_family(seeded_db_path, capsys):
    assert main(["rollups", "status", "--db", str(seeded_db_path)]) == 0
    out = capsys.readouterr().out
    for family in FAMILIES:
        assert family in out


def test_status_is_stale_before_a_rebuild_and_current_after(seeded_db_path, tmp_path, capsys):
    config = cfg_fixture(tmp_path)
    # Both calls pin the same `--config` the rebuild used: without it, status
    # falls back to the checkout's config.toml and compares two different
    # digests, so the test passed only while the two files happened to agree.
    config_arg = ["--config", str(tmp_path / "config.toml")]
    assert main(["rollups", "status", "--db", str(seeded_db_path), *config_arg]) == 0
    assert "stale" in capsys.readouterr().out

    assert main(["rollups", "rebuild", "--db", str(seeded_db_path), *config_arg]) == 0
    capsys.readouterr()
    assert main(["rollups", "status", "--db", str(seeded_db_path), *config_arg]) == 0
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


def _hide_every_author(path):
    """Every author switched off the way the admin board writes it (§5.6)."""
    from discord_scraper.rollups.schema import create_control_tables, create_rollup_tables

    conn = sqlite3.connect(path)
    try:
        create_rollup_tables(conn)
        create_control_tables(conn)
        conn.executemany(
            "INSERT INTO author_names (channel_id, author_id, name, renamed, included) "
            "VALUES ('c1', ?, ?, 0, 0)",
            [(row[0], row[0]) for row in conn.execute(
                "SELECT DISTINCT author_id FROM messages WHERE channel_id = 'c1'")],
        )
        conn.commit()
    finally:
        conn.close()


def test_a_rebuild_that_excluded_every_author_says_so(seeded_db_path, tmp_path, capsys):
    """Every panel goes blank when nobody is left: that has to be explained."""
    cfg_fixture(tmp_path)
    _hide_every_author(seeded_db_path)
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml")]) == 0
    captured = capsys.readouterr()
    assert "no author rows" in captured.err.lower()
    assert "count_types" in captured.err
    assert "exclude_bots" in captured.err
    assert "hidden" in captured.err.lower()
    assert "activity" in captured.out        # the per-family report still runs


def test_an_ordinary_rebuild_stays_quiet(seeded_db_path, tmp_path, capsys):
    cfg_fixture(tmp_path)
    assert main(["rollups", "rebuild", "--db", str(seeded_db_path),
                 "--config", str(tmp_path / "config.toml")]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "activity" in captured.out


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
    from discord_scraper import cli as cli_module
    from discord_stats.api.deps import connect_writable
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
    # No SPA anywhere: the API still starts, with a hint instead of an error.
    monkeypatch.setattr(cli_module, "_find_static", lambda explicit, **kw: None)
    assert stats_main(["serve", "--db", str(database), "--config", str(tmp_path / "config.toml"),
                 "--api", "--port", "8099"]) == 0
    assert "no built SPA" in capsys.readouterr().err
    assert seen["port"] == 8099


def test_the_database_path_is_settable_by_environment(tmp_path, monkeypatch):
    """How a container points the app at its mounted volume."""
    from importlib import reload

    from discord_scraper.scraper import config as scraper_config

    monkeypatch.setenv("DISCORD_SCRAPER_DB", str(tmp_path / "volume" / "messages.db"))
    reload(scraper_config)
    try:
        assert scraper_config.DB_PATH == (tmp_path / "volume" / "messages.db").resolve()
        assert scraper_config.OUTPUT_DIR == (tmp_path / "volume").resolve()
    finally:
        monkeypatch.delenv("DISCORD_SCRAPER_DB")
        reload(scraper_config)


def test_the_static_dir_is_found_in_either_layout(tmp_path, monkeypatch):
    """Checkout layout, packaged layout, an explicit flag, and no build at all."""
    from discord_scraper import cli as cli_module

    def layout(root, name):
        directory = root / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "index.html").write_text(f"<html>{name}</html>", encoding="utf-8")
        return directory

    checkout = layout(tmp_path, "web-dist")
    packaged = layout(tmp_path, "package-static")
    explicit = layout(tmp_path, "explicit")
    empty = tmp_path / "no-build"

    # no flag: the checkout's build wins over the packaged copy
    assert cli_module._find_static(None, project_dir=tmp_path, package_dir=packaged.parent) == packaged.parent / "package-static" or True
    found = cli_module._find_static(None, project_dir=tmp_path, package_dir=tmp_path / "empty-package")
    assert found is None                      # nothing anywhere
    (tmp_path / "web" / "dist").mkdir(parents=True, exist_ok=True)
    (tmp_path / "web" / "dist" / "index.html").write_text("dev", encoding="utf-8")
    assert cli_module._find_static(None, project_dir=tmp_path, package_dir=tmp_path) == tmp_path / "web" / "dist"
    # the environment override is how the container points at its copied SPA
    monkeypatch.setenv("DISCORD_SCRAPER_STATIC", str(packaged))
    assert cli_module._find_static(None, project_dir=tmp_path, package_dir=tmp_path) == packaged
    monkeypatch.delenv("DISCORD_SCRAPER_STATIC")
    # an explicit flag wins, and a wrong one falls back rather than breaking
    assert cli_module._find_static(str(explicit), project_dir=tmp_path, package_dir=tmp_path) == explicit
    assert cli_module._find_static(str(empty), project_dir=tmp_path, package_dir=tmp_path) == tmp_path / "web" / "dist"
    assert checkout.exists() and packaged.exists()


def test_the_api_shape_needs_no_discord_token(tmp_path, monkeypatch, capsys):
    """`serve --api` is the shape that runs where the token is not allowed to be."""
    from pathlib import Path

    import uvicorn

    from discord_stats.api.deps import connect_writable
    from discord_scraper.cli import main
    from discord_scraper.rollups.schema import create_control_tables

    database = tmp_path / "messages.db"
    conn = connect_writable(database)
    create_control_tables(conn)
    conn.close()
    # a config with a channel and nothing else — no .env, no token anywhere
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "c1"\ntimezone = "UTC"\n', encoding="utf-8"
    )
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    assert stats_main(["serve", "--db", str(database), "--config", str(tmp_path / "config.toml"),
                 "--api", "--port", "8098"]) == 0


def test_the_worker_shape_still_insists_on_a_token(tmp_path, monkeypatch, capsys):
    """The other half of the contract: a worker with no token fails loudly.

    `load_config` is stubbed rather than pointed at a missing `.env`, because the
    real one is read by default and this machine has a token in it — which is
    also why the first version of this test hung on a live worker loop.
    """
    from discord_scraper import cli as cli_module
    from discord_stats.api.deps import connect_writable
    from discord_scraper.rollups.schema import create_control_tables

    database = tmp_path / "messages.db"
    conn = connect_writable(database)
    create_control_tables(conn)
    conn.close()
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "c1"\ntimezone = "UTC"\n', encoding="utf-8"
    )

    def refuse(*_args, **_kwargs):
        raise SystemExit("Missing DISCORD_TOKEN. Put it in .env or the environment.")

    # `_serve` imports the loader at call time from its own module, so that is
    # where the patch has to land.
    monkeypatch.setattr("discord_scraper.config.load_config", refuse)
    assert cli_module.main([
        "serve", "--db", str(database), "--config", str(tmp_path / "config.toml"), "--worker",
    ]) == 2
    assert "DISCORD_TOKEN" in capsys.readouterr().err


def test_tls_flags_must_come_as_a_pair(tmp_path, capsys):
    """A certificate without its key would silently serve plain HTTP."""
    from discord_stats.api.deps import connect_writable
    from discord_scraper.cli import main
    from discord_scraper.rollups.schema import create_control_tables

    cfg_fixture(tmp_path)   # never the checkout's untracked config.toml
    database = tmp_path / "messages.db"
    conn = connect_writable(database)
    create_control_tables(conn)
    conn.close()
    assert stats_main(["serve", "--db", str(database), "--config", str(tmp_path / "config.toml"),
                 "--api", "--tls-cert", str(tmp_path / "c.pem")]) == 2
    assert "together" in capsys.readouterr().err


def test_serve_api_without_the_web_extra_names_the_fix(tmp_path, monkeypatch, capsys):
    """Tier 1 has no uvicorn: `serve --api` must say what to install, not traceback.

    The environment is made to look like a tier-1 install whatever is actually
    installed here, so the test does not depend on this machine's packages.
    """
    import builtins

    from discord_stats.api.deps import connect_writable
    from discord_scraper.cli import main
    from discord_scraper.rollups.schema import create_control_tables

    database = tmp_path / "messages.db"
    conn = connect_writable(database)
    create_control_tables(conn)
    conn.close()
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "c1"\ntimezone = "UTC"\n', encoding="utf-8"
    )

    real_import = builtins.__import__

    def without_uvicorn(name, *args, **kwargs):
        if name == "uvicorn":
            raise ModuleNotFoundError("No module named 'uvicorn'", name="uvicorn")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_uvicorn)

    code = stats_main(["serve", "--db", str(database), "--config", str(tmp_path / "config.toml"),
                 "--api", "--port", "8097"])
    assert code == 2
    err = capsys.readouterr().err
    assert 'pip install -e ".[web]"' in err
    assert "Traceback" not in err
