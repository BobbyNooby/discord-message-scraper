"""Command line entry point.

``scrape`` and ``analyze`` work with no optional dependencies, which is the
whole point of the base install. ``rollups`` inspects the data layer and
``serve --worker`` runs ingest; the HTTP server belongs to the dashboard
(``discord-stats serve``), so this module imports no web code.
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
        help="messages per request, 1-100 (default: source.batch_size, else 50)",
    )
    scrape.add_argument(
        "--config", default=None,
        help="config.toml to read (default: the checkout's)",
    )
    scrape.add_argument(
        "--ignore-overrides", action="store_true",
        help="scrape exactly what config.toml says, ignoring stored operator overrides",
    )

    analyze = sub.add_parser(
        "analyze",
        help="run the analysis CLI",
        description="Run the text analysis CLI: `discord-scraper analyze <command>`. "
                    "With no command it writes the default report.",
    )
    analyze.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="analysis command and its options: stats, activity, top-authors, "
             "reactions, attachments, mentions, most-replied, search, top-words, "
             "emoji, report",
    )

    rollups = sub.add_parser("rollups", help="build and inspect the rollup tables")
    actions = rollups.add_subparsers(dest="rollup_command", metavar="<action>")
    rebuild = actions.add_parser("rebuild", help="rebuild every rollup family from messages")
    status = actions.add_parser("status", help="row counts and staleness per family")
    for action in (rebuild, status):
        action.add_argument("--db", required=True, help="path to messages.db")
        action.add_argument("--channel", default=None,
                            help="channel id (default: the only channel in the database)")
    for action in (rebuild, status):
        action.add_argument("--config", default=None,
                            help="config.toml to compare against (default: the checkout's)")
    rebuild.add_argument(
        "--ignore-overrides", action="store_true",
        help="rebuild exactly what config.toml says, ignoring stored operator overrides",
    )
    serve = sub.add_parser(
        "serve", help="run the ingest worker (the dashboard is served by discord-stats)"
    )
    serve.add_argument("--db", default=None, help="path to messages.db (default: output/)")
    serve.add_argument("--config", default=None, help="path to config.toml")
    serve.add_argument(
        "--worker",
        action="store_true",
        help="run the ingest worker (the only shape this command serves)",
    )

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
        if args.config is not None:
            forwarded += ["--config", args.config]
        if args.ignore_overrides:
            forwarded.append("--ignore-overrides")
        return pipeline.main(forwarded)

    if args.command == "analyze":
        from discord_scraper.analysis.cli import main as analysis_main

        return analysis_main(args.args) or 0

    if args.command == "rollups":
        return _rollups(args)

    if args.command == "serve":
        return _serve(args)

    parser.print_help()
    return 0


def _connect(path: str):
    """The CLI's writable handle — the same one the API and worker use.

    Was a hand-rolled copy of this, which is how two writers ended up with
    different pragmas.
    """
    from discord_scraper.db import connect_writable

    return connect_writable(path)
def _has_table(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _channels(conn) -> list[str]:
    return [row[0] for row in conn.execute("SELECT DISTINCT channel_id FROM messages")]


def _channel_of(conn, explicit: str | None) -> tuple[str | None, str | None]:
    """``(channel, error)``: the flag, else the database's only channel.

    A database holding several channels has no obvious answer, so it says so
    rather than reporting on an arbitrary one.
    """
    if explicit:
        return explicit, None
    found = _channels(conn)
    if not found:
        return None, "no messages"
    if len(found) > 1:
        return None, f"database holds {len(found)} channels; pass --channel"
    return found[0], None


def _report(family: str, width: int, rows: int, state: str) -> None:
    print(f"{family:<{width}}  {rows:>7} rows  {state}")


def _rollups_status(args) -> int:
    from pathlib import Path

    from discord_scraper.rollups.build import config_digest
    from discord_scraper.rollups.schema import FAMILIES, is_stale

    if not Path(args.db).exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        digest = _digest_for(args.config, conn)
        channel, problem = _channel_of(conn, args.channel)
        if channel is None:
            print(f"{problem} in {args.db}")
            return 0
        width = max(len(family) for family in FAMILIES)
        built = _has_table(conn, "rollup_meta")
        for family in FAMILIES:
            table = f"rollup_{family}"
            rows = (
                conn.execute(f"SELECT COUNT(*) FROM {table} WHERE channel_id = ?",
                             (channel,)).fetchone()[0]
                if _has_table(conn, table) else 0
            )
            stale = not built or is_stale(conn, channel, family, config_hash=digest)
            _report(family, width, rows, "stale" if stale else "current")
        return 0
    finally:
        conn.close()


def _digest_for(config_path: str | None, conn) -> str | None:
    """The word-list digest, or ``None`` when there is no usable config.

    ``status`` stays useful without one (source-row staleness still works), so a
    missing or unreadable config is not an error here.
    """
    from pathlib import Path

    from discord_scraper.config import PROJECT_DIR, config_from_settings, load_settings
    from discord_scraper.rollups.build import config_digest

    path = Path(config_path) if config_path else PROJECT_DIR / "config.toml"
    try:
        return config_digest(config_from_settings(load_settings(config_path=path)), conn)
    except SystemExit:
        return None


def _rollups_rebuild(args) -> int:
    from dataclasses import replace
    from pathlib import Path

    from discord_scraper.config import PROJECT_DIR, config_from_settings, load_settings
    from discord_scraper.rollups.build import empty_author_rollups, rebuild_all
    from discord_scraper.rollups.schema import FAMILIES

    if not Path(args.db).exists():
        print(f"no database at {args.db}", file=sys.stderr)
        return 2
    config_path = Path(args.config) if args.config else PROJECT_DIR / "config.toml"
    try:
        settings = load_settings(config_path=config_path)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    conn = _connect(args.db)
    try:
        # The operator overrides are what the API compares the recorded zone
        # against, so a rebuild reads them too — otherwise the guard can never
        # be satisfied. `--ignore-overrides` is the deliberate way out, and an
        # explicit `--channel` beats a stored channel override.
        cfg = config_from_settings(settings)
        if not args.ignore_overrides:
            from discord_scraper import overrides

            cfg = overrides.effective_for(conn, cfg)
        if args.channel:
            cfg = replace(cfg, settings=replace(cfg.settings, channel_id=args.channel))
        from discord_scraper import locks

        # A rebuild started while the worker runs one is the collision that
        # bricked the live board: refuse it, name the holder, and never let it
        # fall through to a `database is locked` mid-write.
        try:
            with locks.hold(args.db, kind="rebuild", holder="command line rebuild"):
                counts = rebuild_all(conn, cfg)
        except locks.Busy as exc:
            print(str(exc), file=sys.stderr)
            return 2
    finally:
        conn.close()

    width = max(len(family) for family in FAMILIES)
    for family in FAMILIES:
        _report(family, width, counts[family], "rebuilt")
    if empty_author_rollups(counts):
        # The rebuild is legitimate, so it still exits 0; but every panel keyed
        # on a person is now blank, and the operator deserves to be told why.
        print(
            "warning: no author rows were written — every message is excluded by "
            "source.count_types, exclude_bots or a hidden identity",
            file=sys.stderr,
        )
    return 0


def _rollups(args) -> int:
    if args.rollup_command == "rebuild":
        return _rollups_rebuild(args)
    if args.rollup_command == "status":
        return _rollups_status(args)
    build_parser().parse_args(["rollups", "--help"])
    return 0


def _serve(args) -> int:
    """Run the ingest worker.

    HTTP belongs to the dashboard (``discord-stats serve``), so the tool keeps
    only this shape and the Discord token can live on a machine that serves
    nothing (spec §4.6).
    """
    from pathlib import Path

    from discord_scraper.config import PROJECT_DIR, load_config
    from discord_scraper.scraper.config import DB_PATH

    if not args.worker:
        # Refuse rather than silently do nothing: there is no HTTP left here.
        print(
            "the dashboard is served by the discord-stats package — "
            "run: discord-stats serve --api",
            file=sys.stderr,
        )
        return 2

    database = Path(args.db) if args.db else DB_PATH
    if not database.exists():
        print(f"no database at {database}", file=sys.stderr)
        return 2
    config_path = Path(args.config) if args.config else PROJECT_DIR / "config.toml"
    try:
        cfg = load_config(config_path=config_path)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    from discord_scraper import locks
    from discord_scraper.db import connect_writable
    from discord_scraper.worker.loop import run_forever

    conn = connect_writable(database)
    try:
        # One ingest worker per database. Three of these racing on the same
        # file is how every rebuild started failing; the kernel releases the
        # lock if this process dies, so there is no stale lock to clear.
        try:
            with locks.hold(database, kind="worker", holder="serve --worker"):
                run_forever(conn, cfg)
        except locks.Busy as exc:
            print(str(exc), file=sys.stderr)
            return 2
    finally:
        conn.close()
    return 0


def _find_static(explicit: str | None, *, project_dir=None, package_dir=None) -> Path | None:
    """The built SPA, wherever this install keeps it.

    Checkout: ``web/dist``. Container or wheel: ``static/`` beside the package.
    An explicit ``--static-dir`` wins, and a missing build is a hint rather than
    an error — the API is useful without the SPA.
    """
    import os
    from pathlib import Path

    from discord_scraper.scraper.config import PROJECT_DIR

    home = Path(project_dir) if project_dir is not None else PROJECT_DIR
    package = Path(package_dir) if package_dir is not None else Path(__file__).resolve().parent
    override = os.environ.get("DISCORD_SCRAPER_STATIC")
    candidates = [
        Path(explicit) if explicit else None,
        Path(override) if override else None,
        home / "web" / "dist",
        package / "static",
        home / "static",
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "index.html").is_file():
            return candidate
    return None


