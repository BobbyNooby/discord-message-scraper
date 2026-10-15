"""Paths must survive the package move.

`scraper/config.py` used to sit at the repo root, so `Path(__file__).parent.parent`
was the checkout. Inside `src/discord_scraper/` that same expression resolves to
the package directory, and the 740 MB database stops being found.
"""
from __future__ import annotations

from discord_scraper.scraper.config import DB_PATH, OUTPUT_DIR, resolve_home


def test_resolve_home_finds_the_checkout_root():
    root = resolve_home({})
    assert (root / "pyproject.toml").is_file(), root
    assert (root / "src").is_dir(), root


def test_resolve_home_prefers_the_env_override(tmp_path):
    assert resolve_home({"DISCORD_SCRAPER_HOME": str(tmp_path)}) == tmp_path


def test_database_lives_under_the_checkout_output_dir():
    root = resolve_home({})
    assert OUTPUT_DIR == root / "output"
    assert DB_PATH == root / "output" / "messages.db"
    # the bug: the package directory must never be mistaken for the checkout
    assert DB_PATH.parent.parent != root / "src"
