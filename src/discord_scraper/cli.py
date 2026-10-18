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
    serve = sub.add_parser("serve", help="run the web API, the ingest worker and the SPA")
    serve.add_argument("--db", default=None, help="path to messages.db (default: output/)")
    serve.add_argument("--config", default=None, help="path to config.toml")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    shape = serve.add_mutually_exclusive_group()
    shape.add_argument("--all", action="store_true",
                       help="API + ingest worker in one process (default)")
    shape.add_argument("--api", action="store_true",
                       help="HTTP only; run the worker elsewhere (no Discord token needed here)")
    shape.add_argument("--worker", action="store_true",
                       help="ingest only; serves no HTTP (keep the token off the web host)")
    serve.add_argument("--static-dir", default=None,
                       help="path to the built SPA (default: web/dist)")
    serve.add_argument("--dev-login", action="store_true",
                       help="DEV ONLY: enable /auth/dev-login, which signs anyone in")
    serve.add_argument("--tls-cert", default=None,
                       help="TLS certificate (PEM) — serve HTTPS, e.g. for a tailnet URL")
    serve.add_argument("--tls-key", default=None, help="TLS private key (PEM)")

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

    if args.command == "rollups":
        return _rollups(args)

    if args.command == "serve":
        return _serve(args)

    parser.print_help()
    return 0


def _connect(path: str):
    import sqlite3

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


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
        counts = rebuild_all(conn, cfg)
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
    """Run the API, with the ingest worker beside it.

    One process, two roles: the worker is the only writer and the API the only
    reader, and the worker gets its own connection so a long rebuild never holds
    a handle the request path needs.
    """
    from pathlib import Path

    from discord_scraper.config import PROJECT_DIR, load_config
    from discord_scraper.scraper.config import DB_PATH

    database = Path(args.db) if args.db else DB_PATH
    if not database.exists():
        print(f"no database at {database}", file=sys.stderr)
        return 2
    config_path = Path(args.config) if args.config else PROJECT_DIR / "config.toml"
    try:
        # `--api` serves no worker, so it must not *demand* a Discord token — but
        # it must still read the other secrets, or the admin list and the OAuth
        # client secret vanish from exactly the shape meant to run without a token.
        cfg = load_config(config_path=config_path, require_token=not args.api)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.worker:
        # No HTTP at all: the split exists so the Discord token can live on a
        # machine that serves nothing (spec §4.6).
        from discord_scraper.api.deps import connect_writable
        from discord_scraper.worker.loop import run_forever

        conn = connect_writable(database)
        try:
            run_forever(conn, cfg)
        finally:
            conn.close()
        return 0

    import threading

    import uvicorn

    from discord_scraper.api.app import create_app

    static = _find_static(args.static_dir)
    if static is None:
        print(
            "no built SPA found — serving the API only "
            "(build it with: cd web && pnpm build)",
            file=sys.stderr,
        )

    if args.dev_login:
        print(
            "WARNING: --dev-login is on. Anyone who can reach this port can sign in as "
            "viewer or admin (/auth/dev-login). Never use this on a public host.",
            file=sys.stderr,
        )
    if not args.api:
        threading.Thread(
            target=_worker_thread, args=(cfg, database), daemon=True, name="ingest-worker"
        ).start()

    if bool(args.tls_cert) != bool(args.tls_key):
        # Half a TLS config is a silent downgrade waiting to happen.
        print("--tls-cert and --tls-key must be given together", file=sys.stderr)
        return 2

    uvicorn.run(
        create_app(cfg, database, static_dir=static, dev_login=args.dev_login),
        host=args.host,
        port=args.port,
        ssl_certfile=args.tls_cert,
        ssl_keyfile=args.tls_key,
    )
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


def _worker_thread(cfg, database) -> None:
    """Own connection, own loop — a restart of the API does not orphan a job."""
    from discord_scraper.api.deps import connect_writable
    from discord_scraper.worker.loop import run_forever

    conn = connect_writable(database)
    try:
        run_forever(conn, cfg)
    finally:
        conn.close()
