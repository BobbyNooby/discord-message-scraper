"""Configuration: constants, paths, and batch-size validation."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

API_BASE = "https://discord.com/api/v9"
BATCH_SIZE = 50
BATCH_SIZE_MAX = 100  # Discord's hard per-request limit
REQUEST_DELAY_S = 0.8
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

def resolve_home(env: dict | None = None) -> Path:
    """The checkout root — deliberately not the package directory.

    This module now lives at ``<root>/src/discord_scraper/scraper/config.py``,
    so counting parents from ``__file__`` would put ``output/`` inside the
    package and orphan an existing database. ``DISCORD_SCRAPER_HOME`` wins;
    otherwise walk up for ``pyproject.toml``; otherwise fall back to the
    current working directory.
    """
    env = os.environ if env is None else env
    override = env.get("DISCORD_SCRAPER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").is_file():
            return parent
    return Path.cwd()


def resolve_db_path(home: Path | None = None, env: dict | None = None) -> Path:
    """Where the database lives. ``DISCORD_SCRAPER_DB`` wins.

    A container mounts a volume and points this at it, which is why the path is
    settable at all: everything else in the deployment is derived from it.
    """
    env = os.environ if env is None else env
    override = env.get("DISCORD_SCRAPER_DB")
    if override:
        return Path(override).expanduser().resolve()
    return (home or PROJECT_DIR) / "output" / "messages.db"


PROJECT_DIR = resolve_home()
DB_PATH = resolve_db_path()
OUTPUT_DIR = DB_PATH.parent


@dataclass
class Config:
    token: str
    channel_id: str
    batch_size: int


def normalize_batch_size(value) -> int:
    """Validate the per-request message count (Discord caps at 100).

    Called for ``source.batch_size`` in config.toml and for the ``--batch-size``
    flag, so a bad value names both — not an environment variable, which is not
    where this setting lives.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise SystemExit(
            f"Invalid batch size: {value!r} — must be a whole number "
            "(config.toml source.batch_size, or --batch-size)"
        )
    if not 1 <= n <= BATCH_SIZE_MAX:
        raise SystemExit(
            f"batch size must be 1-{BATCH_SIZE_MAX} "
            f"(config.toml source.batch_size, or --batch-size; "
            f"Discord's API maximum is {BATCH_SIZE_MAX})"
        )
    return n
