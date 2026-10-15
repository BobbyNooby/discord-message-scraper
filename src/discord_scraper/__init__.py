"""Discord Scraper — scrape a Discord channel into SQLite, then analyse it.

Tier 1 (this package, no extras) is the scraper and the analysis CLI.
The web UI and the FastAPI service live behind the ``web`` extra.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
