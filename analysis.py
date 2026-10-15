"""Compatibility entry point for the default analysis report.

The implementation lives in ``src/discord_scraper/analysis/``. This shim puts
``src/`` on the import path so ``python3 analysis.py`` keeps working with no
install step. The full CLI is:

    discord-scraper analyze stats
    discord-scraper analyze activity --by month
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from discord_scraper.analysis import report  # noqa: E402

if __name__ == "__main__":
    report()
