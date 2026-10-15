"""Precomputed rollups.

Everything here is derived from ``messages`` and rebuildable from scratch. The
API reads these tables and never aggregates on request.
"""

from discord_scraper.rollups.schema import (
    FAMILIES,
    create_control_tables,
    create_rollup_tables,
    is_stale,
    provenance,
    set_provenance,
)

__all__ = [
    "FAMILIES",
    "create_control_tables",
    "create_rollup_tables",
    "is_stale",
    "provenance",
    "set_provenance",
]
