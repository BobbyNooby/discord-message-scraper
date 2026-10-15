"""Test bootstrap.

Makes ``src/`` importable so the suite runs from a bare checkout with no
install step (matching the ``pythonpath`` setting in pyproject.toml, and
covering ``python -m unittest`` as well, which does not read that).
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
