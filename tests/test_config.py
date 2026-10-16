"""Secrets come from the environment; settings come from config.toml.

The two must never mix: a token in a committed file is a leaked token, and a
timezone in a secret store is an operator setting nobody can change.
"""
from __future__ import annotations

from discord_scraper.config import get_config, load_config


def _env(tmp_path, monkeypatch, **values):
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return tmp_path / ".env"


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
