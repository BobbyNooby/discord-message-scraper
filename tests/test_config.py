"""Secrets come from the environment; settings come from config.toml.

The two must never mix: a token in a committed file is a leaked token, and a
timezone in a secret store is an operator setting nobody can change.
"""
from __future__ import annotations

import pytest

from discord_scraper.config import get_config, load_config


def _env(tmp_path, monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return tmp_path / ".env"


def _reject(tmp_path, monkeypatch, extra):
    """Load a config made of a valid ``[source]`` plus ``extra``; return the message.

    ``extra`` is appended inside the ``[source]`` table, so a case adds its own
    table header when it needs another section.
    """
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "1"\n' + extra, encoding="utf-8"
    )
    with pytest.raises(SystemExit) as exc:
        load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")
    return str(exc.value)


def test_secrets_come_from_the_environment_not_the_toml(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, DISCORD_TOKEN="tok", ADMIN_DISCORD_ID="42")
    (tmp_path / "config.toml").write_text(
        '[app]\nname = "X"\n[source]\nchannel_id = "1"\ntimezone = "UTC"\n'
    )
    cfg = load_config(env_path=env, config_path=tmp_path / "config.toml")
    assert cfg.secrets.token == "tok"
    assert cfg.secrets.admin_id == "42"
    assert cfg.app_name == "X"


def test_defaults_are_neutral(tmp_path, monkeypatch):
    """Nothing in the defaults may betray which chat this was written for."""
    env = _env(tmp_path, monkeypatch, DISCORD_TOKEN="t")
    (tmp_path / "config.toml").write_text('[source]\nchannel_id = "1"\n')
    cfg = load_config(env_path=env, config_path=tmp_path / "config.toml")
    assert cfg.app_name == "Discord Scraper"
    assert cfg.timezone == "UTC"
    assert cfg.count_types == (0, 19)
    assert cfg.exclude_bots is True
    assert cfg.stopword_packs == ["en"]
    assert cfg.ignore_words == []
    assert cfg.week_start == "monday"
    assert cfg.show_recent == 5


def test_scraper_settings_are_still_present(tmp_path, monkeypatch):
    """The scraper and the existing catch-up tests need these to survive."""
    env = _env(tmp_path, monkeypatch, DISCORD_TOKEN="t")
    (tmp_path / "config.toml").write_text('[source]\nchannel_id = "7"\n')
    cfg = load_config(env_path=env, config_path=tmp_path / "config.toml")
    assert cfg.channel_id == "7"
    assert cfg.batch_size == 50
    assert cfg.token == "t"


def test_a_missing_token_fails_loudly_and_names_the_key(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    (tmp_path / "config.toml").write_text('[source]\nchannel_id = "1"\n')
    try:
        load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")
    except SystemExit as exc:
        assert "DISCORD_TOKEN" in str(exc)
    else:
        raise AssertionError("expected SystemExit for a missing token")


def test_get_config_is_cached(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch, DISCORD_TOKEN="t")
    (tmp_path / "config.toml").write_text('[source]\nchannel_id = "1"\n')
    first = get_config(env_path=env, config_path=tmp_path / "config.toml")
    second = get_config(env_path=env, config_path=tmp_path / "config.toml")
    assert first is second


def test_the_example_config_actually_loads(tmp_path):
    """A template that does not parse is worse than no template.

    This is the file every fork copies, so it is loaded the same way the app
    loads a real one — with the channel id filled in, since that one value
    genuinely has no sane default.
    """
    import shutil

    from discord_scraper.config import load_settings

    example = shutil.copy("config.example.toml", tmp_path / "config.toml")
    text = example.read_text().replace('channel_id = ""', 'channel_id = "123"')
    example.write_text(text)
    # The template names ``dir = "./stopwords.d"``; a deployment that keeps it
    # has to create the directory, and validation insists on that.
    (tmp_path / "stopwords.d").mkdir()

    settings = load_settings(config_path=example)
    assert settings.channel_id == "123"
    assert settings.timezone == "UTC"
    assert settings.count_types == (0, 19)
    assert settings.exclude_bots is True
    assert settings.client_id == ""
    assert settings.redirect_uri.endswith("/auth/callback")
    assert settings.cron_hours == 6


def test_the_web_extra_lists_everything_the_server_imports():
    """A hand-installed dependency is not a declared one.

    `python-multipart` was installed into the dev venv by hand, so every test
    passed while a clean install and the container both died on the admin
    upload's form parsing. The extra is the contract; this pins it.
    """
    import tomllib
    from pathlib import Path

    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    extra = " ".join(project["optional-dependencies"]["web"])
    for package in ("fastapi", "uvicorn", "httpx", "itsdangerous", "apscheduler",
                    "python-multipart"):
        assert package in extra, package
    # tier 1 stays dependency-free
    assert project["dependencies"] == []


def test_the_api_only_shape_still_reads_the_other_secrets(tmp_path, monkeypatch):
    """No token required — but not "no secrets read" either.

    `serve --api` runs where the Discord token is not allowed to be, so it must
    not demand one. The first version of that fix rebuilt the config from
    settings alone, which silently emptied the OAuth client secret and the admin
    id in exactly the shape meant to serve the site.
    """
    from discord_scraper.config import load_config

    for key in ("DISCORD_TOKEN", "DISCORD_CLIENT_SECRET", "ADMIN_DISCORD_ID"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / ".env").write_text(
        "DISCORD_CLIENT_SECRET=shh\nADMIN_DISCORD_ID=424242424242424242\n", encoding="utf-8"
    )
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "c1"\n[access]\nclient_id = "123"\n', encoding="utf-8"
    )

    cfg = load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml",
                      require_token=False)
    assert cfg.token == ""
    assert cfg.client_secret == "shh"
    assert cfg.admin_id == "424242424242424242"
    assert cfg.client_id == "123"


def test_requiring_the_token_is_still_the_default(tmp_path, monkeypatch):
    import pytest

    from discord_scraper.config import load_config

    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.toml").write_text('[source]\nchannel_id = "c1"\n', encoding="utf-8")
    with pytest.raises(SystemExit):
        load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")


# --- values that would otherwise only fail later ------------------------------


def test_a_timezone_that_is_not_iana_is_rejected(tmp_path, monkeypatch):
    """``GMT+8`` looks like a zone but ``ZoneInfo`` cannot name it."""
    message = _reject(tmp_path, monkeypatch, 'timezone = "GMT+8"\n')
    assert "source.timezone" in message
    assert "IANA" in message


def test_a_negative_show_recent_is_rejected(tmp_path, monkeypatch):
    """SQLite reads ``LIMIT -1`` as no limit, so this would dump the channel."""
    message = _reject(tmp_path, monkeypatch, "[ui]\nshow_recent = -1\n")
    assert "ui.show_recent" in message
    assert "200" in message


@pytest.mark.parametrize("hours", [0, 99])
def test_cron_hours_outside_1_to_24_is_rejected(tmp_path, monkeypatch, hours):
    """The admin PUT already bounds this; a hand-edited file must match."""
    message = _reject(tmp_path, monkeypatch, f"[worker]\ncron_hours = {hours}\n")
    assert "worker.cron_hours" in message
    assert "24" in message


def test_empty_count_types_is_rejected(tmp_path, monkeypatch):
    """An empty list counts nothing and writes empty rollups without complaint."""
    message = _reject(tmp_path, monkeypatch, "count_types = []\n")
    assert "source.count_types" in message


def test_an_unknown_allowlist_mode_is_rejected(tmp_path, monkeypatch):
    message = _reject(tmp_path, monkeypatch, '[access]\nallowlist = "whatever"\n')
    assert "access.allowlist" in message
    assert "posted" in message
    assert "explicit" in message


def test_an_unknown_thumbnail_mode_is_rejected(tmp_path, monkeypatch):
    """Every unknown value silently meant ``proxy``, which fetches from a CDN."""
    message = _reject(tmp_path, monkeypatch, '[media]\nthumbnails = "cdn"\n')
    assert "media.thumbnails" in message
    assert "proxy" in message
    assert "off" in message


def test_a_negative_media_top_n_is_rejected(tmp_path, monkeypatch):
    message = _reject(tmp_path, monkeypatch, "[media]\ntop_n = -3\n")
    assert "media.top_n" in message
    assert "0" in message


def test_a_stopwords_dir_that_does_not_exist_is_rejected(tmp_path, monkeypatch):
    message = _reject(tmp_path, monkeypatch, '[stopwords]\ndir = "missing.d"\n')
    assert "stopwords.dir" in message


def test_a_stopwords_dir_that_is_not_a_path_is_rejected(tmp_path, monkeypatch):
    """An int reached ``Path()`` and left as a TypeError traceback, not a
    config error — a hand-edited file must fail the way the others do."""
    message = _reject(tmp_path, monkeypatch, "[stopwords]\ndir = 5\n")
    assert "stopwords.dir" in message
    assert "5" in message


def test_the_documented_auto_timezone_is_still_accepted(tmp_path, monkeypatch):
    """``timezone = "auto"`` means the host zone (``timeutil.resolve_tz``)."""
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "1"\ntimezone = "auto"\n', encoding="utf-8"
    )
    cfg = load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")
    assert cfg.timezone == "auto"


def test_a_fully_valid_config_still_loads(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "t")
    (tmp_path / "stopwords.d").mkdir()
    (tmp_path / "config.toml").write_text(
        '[source]\nchannel_id = "1"\ntimezone = "Asia/Kuala_Lumpur"\n'
        "count_types = [0, 19]\n"
        '[stopwords]\ndir = "./stopwords.d"\n'
        "[worker]\ncron_hours = 24\n"
        "[ui]\nshow_recent = 200\n"
        '[access]\nallowlist = "explicit"\n'
        '[media]\ntop_n = 0\nthumbnails = "off"\n',
        encoding="utf-8",
    )
    cfg = load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")
    assert cfg.timezone == "Asia/Kuala_Lumpur"
    assert cfg.count_types == (0, 19)
    assert cfg.cron_hours == 24
    assert cfg.show_recent == 200
    assert cfg.allowlist == "explicit"
    assert cfg.media_top_n == 0
    assert cfg.media_thumbnails == "off"
    assert cfg.stopword_dir == tmp_path / "stopwords.d"
