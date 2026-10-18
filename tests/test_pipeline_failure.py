"""A failed scrape must not claim progress that was never saved.

After a 401/403 the pipeline printed "Progress is saved — re-run the script to
resume." Nothing was saved and a dead token cannot be resumed, so a re-run loops
forever. The line now appears only when the database actually holds messages.
"""

from __future__ import annotations

import sqlite3
import urllib.error

import pytest

from tests.conftest import cfg_fixture


def _scrape(tmp_path, monkeypatch, db_path, fetch):
    from discord_scraper import config, pipeline
    from discord_scraper.scraper import database

    cfg_fixture(tmp_path)
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(pipeline, "REQUEST_DELAY_S", 0)
    monkeypatch.setattr(pipeline, "fetch_messages", fetch)
    return pipeline.main(["--config", str(tmp_path / "config.toml")])


def _fails(exc):
    def fetch(token, channel_id, before=None, limit=50):
        raise exc
    return fetch


def _empty_db(tmp_path):
    from discord_scraper.scraper.database import SCHEMA

    path = tmp_path / "empty.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


def test_fetch_messages_classifies_a_401_as_an_auth_error(monkeypatch):
    """The classification lives at the HTTP boundary, where the status is known."""
    from discord_scraper.scraper import api

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(api.AuthError):
        api.fetch_messages("dead-token", "c1", None)


def test_fetch_messages_classifies_a_403_as_an_auth_error(monkeypatch):
    from discord_scraper.scraper import api

    def refuse(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, None)

    monkeypatch.setattr("urllib.request.urlopen", refuse)
    with pytest.raises(api.AuthError):
        api.fetch_messages("dead-token", "c1", None)


def test_a_401_never_claims_progress_was_saved(
    seeded_db_path, tmp_path, monkeypatch, capsys
):
    from discord_scraper.scraper.api import AuthError

    code = _scrape(
        tmp_path, monkeypatch, seeded_db_path,
        _fails(AuthError("HTTP 401/403: token invalid or channel not accessible")),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "Stopped" in out
    assert "Progress is saved" not in out


def test_a_non_auth_failure_with_saved_messages_offers_to_resume(
    seeded_db_path, tmp_path, monkeypatch, capsys
):
    code = _scrape(
        tmp_path, monkeypatch, seeded_db_path,
        _fails(RuntimeError("HTTP 500: discord is having a bad day")),
    )
    assert code == 1
    assert "Progress is saved" in capsys.readouterr().out


def test_a_non_auth_failure_with_an_empty_database_says_nothing_was_saved(
    tmp_path, monkeypatch, capsys
):
    code = _scrape(
        tmp_path, monkeypatch, _empty_db(tmp_path),
        _fails(RuntimeError("HTTP 500: discord is having a bad day")),
    )
    assert code == 1
    assert "Progress is saved" not in capsys.readouterr().out
