"""The CLI is the tier-1 interface: it must work with no web extras installed."""
from __future__ import annotations

from discord_scraper.cli import main


def test_cli_exposes_the_four_subcommands(capsys):
    assert main(["--help"]) == 0
    out = capsys.readouterr().out
    for cmd in ("scrape", "analyze", "rollups", "serve"):
        assert cmd in out
