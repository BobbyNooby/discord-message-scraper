"""Ingest must resolve configuration through the one override authority.

The API and the rebuild path both read ``overrides.effective_for``; if ingest
does not, an admin's saved channel changes what the dashboard reads but not what
the scraper fetches — the setting looks like it works and silently does not.
These tests pin the ingest side of that contract for the worker.
"""

from discord_scraper.worker.jobs import claim_next, enqueue, run_job
from tests.conftest import cfg_fixture


def _set_override(conn, key, value):
    conn.execute("INSERT INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def _message(mid):
    """Minimal API message object, shaped for scraper.transform.to_row."""
    return {
        "id": str(mid),
        "channel_id": "1",
        "author": {"id": "u1", "username": "bob"},
        "content": f"msg {mid}",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "type": 0,
        "pinned": False,
    }


def _one_page(fetch_calls):
    """A stubbed Discord that records the channel it was asked for, one page."""

    def fetch(token, channel_id, before=None, limit=50):
        fetch_calls.append(channel_id)
        return [_message(900)] if before is None else []

    return fetch


def test_the_worker_ingest_fetches_the_override_channel(conn, tmp_path, monkeypatch):
    from discord_scraper import pipeline

    cfg = cfg_fixture(tmp_path)                     # the file says "c1"
    _set_override(conn, "channel_id", "c2")
    monkeypatch.setattr(pipeline, "REQUEST_DELAY_S", 0)
    calls = []
    monkeypatch.setattr(pipeline, "fetch_messages", _one_page(calls))

    enqueue(conn, "c1", "catch_up")
    run_job(conn, cfg, claim_next(conn))

    assert calls[0] == "c2"
    stored = {row[0] for row in conn.execute("SELECT id FROM messages")}
    assert "900" in stored


def test_the_worker_ingest_carries_the_whole_effective_config(conn, tmp_path, monkeypatch):
    """Not just the channel: the ingest path hands the resolved config down."""
    from discord_scraper import pipeline

    cfg = cfg_fixture(tmp_path)
    _set_override(conn, "channel_id", "c2")
    _set_override(conn, "allowlist_policy", "explicit")
    _set_override(conn, "ignore_words", '["spam"]')
    captured = {}

    def capture(_conn, ingest_cfg, *_args, **_kwargs):
        captured["cfg"] = ingest_cfg
        return 0

    monkeypatch.setattr(pipeline, "catch_up", capture)

    enqueue(conn, "c1", "catch_up")
    run_job(conn, cfg, claim_next(conn))

    assert captured["cfg"].channel_id == "c2"
    assert getattr(captured["cfg"], "allowlist", None) == "explicit"
    assert getattr(captured["cfg"], "ignore_words", None) == ["spam"]


def test_the_worker_ingest_falls_back_to_the_file(conn, tmp_path, monkeypatch):
    """No override: the file's channel, exactly as before."""
    from discord_scraper import pipeline

    cfg = cfg_fixture(tmp_path)
    monkeypatch.setattr(pipeline, "REQUEST_DELAY_S", 0)
    calls = []
    monkeypatch.setattr(pipeline, "fetch_messages", _one_page(calls))

    enqueue(conn, "c1", "catch_up")
    run_job(conn, cfg, claim_next(conn))

    assert calls[0] == "c1"


def test_the_loop_enqueues_the_scheduled_ingest_for_the_override_channel(
    conn, tmp_path, monkeypatch
):
    from discord_scraper.worker import loop

    cfg = cfg_fixture(tmp_path)
    _set_override(conn, "channel_id", "c2")
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)

    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _seconds: None)

    queued = [row[0] for row in conn.execute("SELECT channel_id FROM jobs")]
    assert queued == ["c2"]
