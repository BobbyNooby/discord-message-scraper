"""Compatibility entry point for ``python3 main.py``.

The implementation lives in ``src/discord_scraper/``. This shim puts ``src/``
on the import path so the documented invocation keeps working with no install
step. Prefer the console script once the package is installed:

    discord-scraper scrape
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from discord_scraper.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
