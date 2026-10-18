"""Compatibility entry point for the analysis CLI.

The implementation lives in ``src/discord_scraper/analysis/``. This shim puts
``src/`` on the import path and forwards its arguments, so the commands work
from a bare checkout with no install step:

    python3 analysis.py stats
    python3 analysis.py activity --by month
    python3 analysis.py            # no command = the default report
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from discord_scraper.analysis.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
