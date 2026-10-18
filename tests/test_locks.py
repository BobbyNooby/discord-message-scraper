"""One rebuild at a time, and one worker per database.

A long write transaction does not just make SQLite wait; past the busy timeout
it fails with ``database is locked``, and a rebuild that fails commits nothing.
Four writers on the live database did exactly that and left the rollups on the
old timezone forever. These locks turn that into a clear refusal that names the
holder, and the kernel releases them when the holding process dies, so a crash
cannot leave a stale lock behind.
"""

import sqlite3

import pytest

from discord_scraper.rollups.schema import create_control_tables, create_rollup_tables
from discord_scraper.worker.jobs import enqueue, run_pending
from tests.conftest import cfg_fixture


def _zones_on_disk(path):
    conn = sqlite3.connect(path)
    try:
        try:
            rows = conn.execute("SELECT DISTINCT timezone FROM rollup_meta").fetchall()
        except sqlite3.OperationalError:
            return set()
        return {str(row[0]) for row in rows}
    finally:
        conn.close()


def test_the_cli_refuses_while_a_rebuild_is_running(seeded_db_path, tmp_path, capsys):
    from discord_scraper import locks
    from discord_scraper.cli import main

    cfg_fixture(tmp_path, timezone="UTC")
    with locks.hold(seeded_db_path, kind="rebuild", holder="worker job #7"):
        code = main(["rollups", "rebuild", "--db", str(seeded_db_path),
                     "--config", str(tmp_path / "config.toml")])
    assert code != 0
    err = capsys.readouterr().err
    assert "already running" in err
    assert "#7" in err
    assert "database is locked" not in err
    assert _zones_on_disk(seeded_db_path) == set()   # the second one never wrote


def test_the_worker_job_fails_cleanly_while_a_rebuild_is_running(
        conn, seeded_db_path, tmp_path):
    from discord_scraper import locks

    cfg = cfg_fixture(tmp_path, timezone="UTC")
    enqueue(conn, "c1", "rebuild_rollups")

    with locks.hold(seeded_db_path, kind="rebuild", holder="command line rebuild"):
        with pytest.raises(locks.Busy) as excinfo:
            run_pending(conn, cfg)

    assert "already running" in str(excinfo.value)
    assert "command line rebuild" in str(excinfo.value)
    row = conn.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "already running" in row["error"]
    assert _zones_on_disk(seeded_db_path) == set()   # nothing half-written


def test_the_rebuild_lock_is_released_for_the_next_run(seeded_db_path):
    from discord_scraper import locks

    with locks.hold(seeded_db_path, kind="rebuild", holder="first"):
        pass
    with locks.hold(seeded_db_path, kind="rebuild", holder="second"):
        pass


def test_a_second_worker_lock_is_refused(seeded_db_path):
    from discord_scraper import locks

    with locks.hold(seeded_db_path, kind="worker", holder="pid 4242"):
        with pytest.raises(locks.Busy) as excinfo:
            with locks.hold(seeded_db_path, kind="worker", holder="pid 9999"):
                pass
    assert "4242" in str(excinfo.value)


def test_serve_refuses_a_second_worker(seeded_db_path, tmp_path, capsys, monkeypatch):
    from discord_scraper import locks
    from discord_scraper.cli import main

    cfg_fixture(tmp_path)
    # The fixture's .env holds the token; the loader must read that one, not the
    # checkout's (a fresh clone has none, and the test must not need one).
    monkeypatch.setattr("discord_scraper.config.PROJECT_DIR", tmp_path)
    # `run_forever` never returns in real life; here it must, so the guard is
    # the only thing under test.
    monkeypatch.setattr("discord_scraper.worker.loop.run_forever",
                        lambda conn, cfg: 0)

    with locks.hold(seeded_db_path, kind="worker", holder="pid 4242"):
        code = main(["serve", "--worker", "--db", str(seeded_db_path),
                     "--config", str(tmp_path / "config.toml")])

    assert code != 0
    err = capsys.readouterr().err
    assert "already running" in err
    assert "4242" in err
