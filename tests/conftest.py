"""Test bootstrap.

Makes ``src/`` importable so the suite runs from a bare checkout with no
install step (matching the ``pythonpath`` setting in pyproject.toml, and
covering ``python -m unittest`` as well, which does not read that).
"""

from __future__ import annotations

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


def cfg_fixture(tmp_path, packs=None, ignore=None, **source):
    """A real ``Config`` built from a throwaway ``.env`` + ``config.toml``.

    A plain function, not a fixture: one test may need several configs. Secrets
    go in the ``.env`` and settings in the TOML — the split the app enforces —
    so this exercises the real loader instead of a stub.
    """
    values = {"channel_id": '"c1"'}
    values.update({k: f'"{v}"' if isinstance(v, str) else str(v) for k, v in source.items()})
    lines = ["[source]", *(f"{k} = {v}" for k, v in values.items())]
    if packs is not None:
        lines.append("[stopwords]")
        lines.append(f"packs = {[str(p) for p in packs]!r}")
    if ignore is not None:
        if "[stopwords]" not in lines:
            lines.append("[stopwords]")
        lines.append(f"ignore = {[str(w) for w in ignore]!r}")
    (tmp_path / "config.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / ".env").write_text("DISCORD_TOKEN=test-token\n", encoding="utf-8")
    return load_config(env_path=tmp_path / ".env", config_path=tmp_path / "config.toml")

