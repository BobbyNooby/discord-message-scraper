"""Analysis toolkit for output/messages.db.

CLI — no coding needed. Installed (`pip install -e .`):
    discord-scraper analyze stats
    discord-scraper analyze activity --by month
    discord-scraper analyze top-authors --limit 10
    discord-scraper analyze reactions
    discord-scraper analyze search "hello"
    discord-scraper analyze                 # no command = the default report

From a bare checkout, the same commands run as a module with ``src/`` on the
path; ``analysis.py`` is the shim for the default report only:
    PYTHONPATH=src python3 -m discord_scraper.analysis stats
    python3 analysis.py

Import — for notebooks / scripts:
    from discord_scraper.analysis import MessageDB
    db = MessageDB()
    db.top_authors(10)          # list[sqlite3.Row]

Every query function returns list[sqlite3.Row] so results compose.
"""

from .cli import main
from .db import REAL_MESSAGES, connect, q
from .messagedb import MessageDB
from .queries import (
    activity,
    attachments,
    emoji_use,
    mentions,
    most_replied,
    reactions,
    search,
    top_authors,
    top_words,
)
from .report import report

__all__ = [
    "REAL_MESSAGES",
    "MessageDB",
    "activity",
    "attachments",
    "connect",
    "emoji_use",
    "main",
    "mentions",
    "most_replied",
    "q",
    "reactions",
    "report",
    "search",
    "top_authors",
    "top_words",
]
