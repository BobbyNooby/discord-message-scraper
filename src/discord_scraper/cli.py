"""Command line entry point.

``scrape`` and ``analyze`` work with no optional dependencies, which is the
whole point of the base install. ``rollups`` and ``serve`` are wired up by
later work; they exist in the parser from the start so the help text is
stable and scriptable.
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="discord-scraper",
        description="Scrape a Discord channel into SQLite and analyse it.",
    )
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    scrape = sub.add_parser("scrape", help="fetch messages into the database")
    scrape.add_argument(
        "--max-batches", type=int, default=None,
        help="stop after this many API batches (useful for a quick test run)",
    )
    scrape.add_argument(
        "--batch-size", type=int, default=None,
        help="messages per request, 1-100 (default: BATCH_SIZE, else 50)",
    )

    analyze = sub.add_parser("analyze", help="run the analysis CLI")
    analyze.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("rollups", help="build and inspect the rollup tables")
    sub.add_parser("serve", help="run the web API and the ingest worker")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:  # argparse exits for --help and for bad usage
        return int(exc.code or 0)

    if args.command == "scrape":
        from discord_scraper import pipeline

        forwarded: list[str] = []
        if args.max_batches is not None:
            forwarded += ["--max-batches", str(args.max_batches)]
        if args.batch_size is not None:
            forwarded += ["--batch-size", str(args.batch_size)]
        return pipeline.main(forwarded)

    if args.command == "analyze":
        from discord_scraper.analysis.cli import main as analysis_main

        return analysis_main(args.args) or 0

    if args.command in ("rollups", "serve"):
        print(f"'{args.command}' is not implemented yet.", file=sys.stderr)
        return 2

    parser.print_help()
    return 0
