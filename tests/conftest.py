"""Test bootstrap.

Makes ``src/`` importable so the suite runs from a bare checkout with no
install step (matching the ``pythonpath`` setting in pyproject.toml, and
covering ``python -m unittest`` as well, which does not read that).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from discord_scraper.config import load_config  # noqa: E402  (SRC first, above)
from discord_scraper.scraper.database import SCHEMA  # noqa: E402  (SRC first, above)


@pytest.fixture
def mem_db():
    """An in-memory database carrying the real ``messages`` schema.

    The rollups read the production table, so a stub schema would let a query
    pass here and fail against the 740 MB original.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    try:
        yield conn
    finally:
        conn.close()


def _toml(value):
    """Render a Python value as TOML. ``repr`` is wrong: ``False``/``'x'``."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml(item) for item in value) + "]"
    return f'"{value}"'


def cfg_fixture(tmp_path, packs=None, ignore=None, identities=None, **source):
    """A real ``Config`` built from a throwaway ``.env`` + ``config.toml``.

    A plain function, not a fixture: one test may need several configs (a
    ``Config`` is a frozen dataclass with nested ``Secrets``/``Settings``, so
    ``dataclasses.replace`` cannot reach a setting and the real loader is the
    only honest way to change one). Secrets go in the ``.env`` and settings in
    the TOML — the split the app enforces.
    """
    values = {"channel_id": '"c1"'}
    values.update({k: _toml(v) for k, v in source.items()})
    lines = ["[source]", *(f"{k} = {v}" for k, v in values.items())]
    if packs is not None or ignore is not None:
        lines.append("[stopwords]")
        if packs is not None:
            lines.append(f"packs = {_toml(list(packs))}")
        if ignore is not None:
            lines.append(f"ignore = {_toml(list(ignore))}")
    if identities:
        lines.append("[identity]")
        for canonical, alts in identities.items():
            lines.append(f"{canonical} = {_toml([str(a) for a in alts])}")
    (tmp_path / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=test-token\n", encoding="utf-8")
    return load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")


# The seeded conversation every rollup test is measured against. Deliberately
# small, and deliberately awkward in the five ways the plan's Review Focus names:
# a null `embeds`, a human with no `bot` key, a bot with one, an author known
# only by a join event, and a display name that changed over time.
def author_raw(author_id, username=None, global_name=None, bot=False, **extra):
    """A Discord message's ``raw`` object: ``{"author": {...}, ...}``.

    ``extra`` carries the top-level arrays (``embeds``, ``reactions``,
    ``attachments``) that the rollups read, so a fixture cannot put them in the
    wrong place — which is exactly the mistake this helper exists to prevent.
    """
    author = {"id": author_id}
    if username:
        author["username"] = username
    if global_name:
        author["global_name"] = global_name
    if bot:
        author["bot"] = True
    return json.dumps({"author": author, **extra})


SEED_MESSAGES = (
    # id, author_id, author_username, content, created_at, edited_at, reply_to_id,
    # has_attachments, type, raw
    ("m1", "u1", "u1handle", "hi", "2026-01-01T10:00:00+00:00", None, None, 0, 0,
     author_raw("u1", "u1handle", "GlobalName", embeds=None)),
    ("m2", "u1", "u1handle", "abc", "2026-01-01T10:01:00+00:00", None, "m1", 0, 19,
     author_raw("u1", "u1handle", "GlobalName")),
    ("m3", "u1", "u1handle", "defghi", "2026-01-01T10:02:00+00:00", "2026-01-01T10:02:45+00:00",
     None, 1, 0, author_raw("u1", "u1handle", "GlobalName")),
    ("m4", "u2", "plain_user", "embed here", "2026-01-02T10:00:00+00:00", None, None, 0, 0,
     author_raw("u2", "plain_user", "OldName",
                embeds=[{"type": "video", "provider": {"name": "YouTube"}}])),
    ("m5", "u2", "plain_user", "hello there", "2026-01-02T11:00:00+00:00", None, None, 0, 0,
     author_raw("u2", "plain_user")),
    ("m6", "bot1", "botty", "beep", "2026-01-03T10:00:00+00:00", None, None, 0, 0,
     author_raw("bot1", "botty", bot=True)),
    ("m7", "u3", None, "", "2026-01-04T10:00:00+00:00", None, None, 0, 7,
     author_raw("u3")),
)


def seed_messages(conn, rows=SEED_MESSAGES, channel_id="c1"):
    """Insert fixture messages into a ``messages`` table."""
    for mid, author, username, content, created, edited, reply_to, attachments, msg_type, raw in rows:
        conn.execute(
            """
            INSERT INTO messages (id, channel_id, author_id, author_username, content,
                                  created_at, edited_at, reply_to_id, has_attachments,
                                  type, pinned, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (mid, channel_id, author, username, content, created, edited, reply_to,
             attachments, msg_type, raw),
        )
    conn.commit()


@pytest.fixture
def seeded_db_path(tmp_path):
    """The seeded conversation in a real file — the CLI takes a path, not a handle."""
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        seed_messages(conn)
    finally:
        conn.close()
    return path


@pytest.fixture
def client(seeded_db_path, tmp_path):
    """An HTTP client over a seeded, **rebuilt** database.

    The rollups are built here because the API reads them and never builds them:
    a server that computed on request would be the design this project rejected.
    """
    from fastapi.testclient import TestClient

    from discord_scraper.api.app import create_app
    from discord_scraper.rollups.build import rebuild_all

    cfg = cfg_fixture(tmp_path)
    conn = sqlite3.connect(seeded_db_path)
    try:
        rebuild_all(conn, cfg)
    finally:
        conn.close()
    return TestClient(create_app(cfg, seeded_db_path))


@pytest.fixture
def cfg(tmp_path):
    """A config for the seeded channel, with the app defaults."""
    return cfg_fixture(tmp_path)


@pytest.fixture
def seeded_db(mem_db):
    """The seeded conversation in an in-memory database."""
    seed_messages(mem_db)
    return mem_db


