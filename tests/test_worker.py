import sqlite3

import pytest

from discord_scraper.rollups.schema import create_control_tables, create_rollup_tables
from discord_scraper.worker.jobs import (
    claim_next,
    enqueue,
    record_worker_seen,
    run_job,
    set_worker_state,
    worker_state,
)
from discord_scraper.worker.probe import probe_token
from discord_scraper.worker.scheduler import next_delay_seconds, schedule_enabled


@pytest.fixture
def two_conns(seeded_db_path):
    """Two connections to one file — the shape of "two workers, one database"."""
    connections = []
    for _ in range(2):
        conn = sqlite3.connect(seeded_db_path)
        conn.row_factory = sqlite3.Row
        create_rollup_tables(conn)
        create_control_tables(conn)
        conn.commit()
        connections.append(conn)
    try:
        yield connections
    finally:
        for conn in connections:
            conn.close()


def _responder(status):
    class Response:
        status_code = status

        def json(self):
            return {"id": "me"}

    return lambda *a, **k: Response()


# --- worker state ------------------------------------------------------------

def test_worker_state_round_trip(two_conns):
    conn, other = two_conns
    assert worker_state(conn, "token_status") is None
    set_worker_state(conn, "token_status", "ok")
    assert worker_state(other, "token_status") == "ok"      # committed, so visible


def test_record_worker_seen(two_conns):
    conn, other = two_conns
    record_worker_seen(conn, "2026-01-01T00:00:00+00:00")
    assert worker_state(other, "last_seen_at") == "2026-01-01T00:00:00+00:00"


# --- token probe -------------------------------------------------------------

def test_probe_marks_a_live_token(two_conns):
    conn, other = two_conns
    ok, status = probe_token(conn, "a-token", get=_responder(200))
    assert (ok, status) == (True, 200)
    assert worker_state(other, "token_status") == "ok"
    assert worker_state(other, "token_checked_at")
    assert worker_state(other, "token_last_ok_at")


def test_probe_marks_a_dead_token(two_conns):
    conn, other = two_conns
    ok, status = probe_token(conn, "a-token", get=_responder(401))
    assert (ok, status) == (False, 401)
    assert worker_state(other, "token_status") == "dead"


def test_probe_distinguishes_403_from_401(two_conns):
    """403 means the channel is unreachable, not that the token is dead (§10.1)."""
    conn, other = two_conns
    probe_token(conn, "a-token", get=_responder(200))
    ok, status = probe_token(conn, "a-token", get=_responder(403))
    assert (ok, status) == (False, 403)
    assert worker_state(other, "token_status") == "ok"


def test_a_dead_token_is_not_resurrected_by_a_later_403(two_conns):
    conn, other = two_conns
    probe_token(conn, "a-token", get=_responder(401))
    probe_token(conn, "a-token", get=_responder(403))
    assert worker_state(other, "token_status") == "dead"


def test_the_worker_imports_without_httpx():
    """The tool declares no dependencies, so the worker must start on a bare Python.

    httpx is installed in the dev environment, which is exactly how a top-level
    import of it went unnoticed; blocking it in a child interpreter is the check.
    """
    import subprocess
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "src"
    code = (
        "import sys; sys.modules['httpx'] = None; "
        f"sys.path.insert(0, {str(src)!r}); "
        "import discord_scraper.worker.jobs, discord_scraper.worker.loop, discord_scraper.cli"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_probe_without_a_fake_uses_the_stdlib_client(two_conns, monkeypatch):
    from discord_scraper.worker import probe

    conn, other = two_conns
    monkeypatch.setattr(probe, "get_current_user", lambda token, timeout: (401, None, "dead"))
    assert probe.probe_token(conn, "a-token") == (False, 401)
    assert worker_state(other, "token_status") == "dead"


def test_probe_survives_a_network_error(two_conns):
    conn, other = two_conns

    def boom(*a, **k):
        raise OSError("no route to host")

    ok, status = probe_token(conn, "a-token", get=boom)
    assert ok is False and status == 0
    assert worker_state(other, "token_status") == "unknown"


# --- the job queue -----------------------------------------------------------

def test_a_job_is_claimed_exactly_once(two_conns):
    a, b = two_conns
    enqueue(a, "c1", "catch_up")
    first, second = claim_next(a), claim_next(b)
    assert first is not None
    assert second is None


def test_claim_marks_the_job_running(two_conns):
    conn, other = two_conns
    enqueue(conn, "c1", "probe")
    job = claim_next(conn)
    assert job["status"] == "running" and job["started_at"]
    assert other.execute("SELECT status FROM jobs").fetchone()[0] == "running"


def test_claim_returns_none_on_an_empty_queue(two_conns):
    conn, _other = two_conns
    assert claim_next(conn) is None


def test_enqueue_returns_the_job_id(two_conns):
    conn, _other = two_conns
    assert enqueue(conn, "c1", "rebuild_rollups") == 1


# --- cancelling a queued job -------------------------------------------------

def test_a_cancelled_job_is_never_claimed(two_conns):
    from discord_scraper.worker.jobs import CANCELLED, cancel_job

    a, b = two_conns
    job_id = enqueue(a, "c1", "rebuild_rollups")
    result = cancel_job(a, job_id)
    assert result["status"] == CANCELLED
    assert result["previous_status"] == "queued"
    assert claim_next(a) is None
    assert claim_next(b) is None


def test_cancelling_a_running_job_is_refused(two_conns):
    from discord_scraper.worker.jobs import JobRunning, cancel_job

    conn, _other = two_conns
    enqueue(conn, "c1", "rebuild_rollups")
    job = claim_next(conn)
    with pytest.raises(JobRunning):
        cancel_job(conn, job["id"])
    assert conn.execute("SELECT status FROM jobs WHERE id = ?",
                        (job["id"],)).fetchone()["status"] == "running"


def test_cancelling_an_unknown_job_is_not_found(two_conns):
    from discord_scraper.worker.jobs import JobNotFound, cancel_job

    with pytest.raises(JobNotFound):
        cancel_job(two_conns[0], 4242)


# --- running jobs ------------------------------------------------------------

def test_run_job_probe_records_state_and_completes(two_conns, cfg):
    conn, other = two_conns
    enqueue(conn, "c1", "probe")
    job = claim_next(conn)
    run_job(conn, cfg, job, get=_responder(200))
    row = other.execute("SELECT status, finished_at, result FROM jobs").fetchone()
    assert row["status"] == "done"
    assert row["finished_at"]
    assert worker_state(other, "token_status") == "ok"


def test_run_job_rebuild_writes_rollups_and_completes(two_conns, cfg):
    conn, other = two_conns
    enqueue(conn, "c1", "rebuild_rollups")
    job = claim_next(conn)
    run_job(conn, cfg, job)
    assert other.execute("SELECT COUNT(*) FROM rollup_activity").fetchone()[0] == 2
    row = other.execute("SELECT status, result FROM jobs").fetchone()
    assert row["status"] == "done"
    assert "15" in row["result"]


def test_run_job_rebuild_records_its_final_phase(two_conns, cfg):
    """A rebuild is the one long job, so it says more than "running"."""
    conn, other = two_conns
    enqueue(conn, "c1", "rebuild_rollups")
    job = claim_next(conn)
    run_job(conn, cfg, job)
    row = other.execute("SELECT status, phase FROM jobs").fetchone()
    assert row["status"] == "done"
    assert row["phase"] == "finished"


def test_set_job_phase_is_visible_to_another_connection(two_conns):
    from discord_scraper.worker.jobs import set_job_phase

    conn, other = two_conns
    enqueue(conn, "c1", "rebuild_rollups")
    job = claim_next(conn)
    set_job_phase(conn, job["id"], "deriving")
    assert other.execute("SELECT phase FROM jobs").fetchone()["phase"] == "deriving"


def test_a_failing_rebuild_job_records_the_failed_phase(two_conns, cfg, monkeypatch):
    import discord_scraper.rollups.build as build

    conn, other = two_conns

    def boom(*args, **kwargs):
        raise RuntimeError("mid-rebuild")

    monkeypatch.setattr(build, "rebuild_word", boom)
    enqueue(conn, "c1", "rebuild_rollups")
    job = claim_next(conn)
    with pytest.raises(RuntimeError):
        run_job(conn, cfg, job)
    row = other.execute("SELECT status, phase FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert row["phase"] == "failed"


def test_run_job_failure_is_recorded_and_raised(two_conns, cfg, monkeypatch):
    conn, other = two_conns
    from discord_scraper.worker import jobs as jobs_module

    def boom(*a, **k):
        raise RuntimeError("discord said no")

    monkeypatch.setitem(jobs_module.DISPATCH, "catch_up", boom)
    enqueue(conn, "c1", "catch_up")
    job = claim_next(conn)
    with pytest.raises(RuntimeError):
        run_job(conn, cfg, job)
    row = other.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "discord said no" in row["error"]


def test_an_ingest_queues_a_rebuild(two_conns, cfg, monkeypatch):
    """§10.5: a rebuild is enqueued automatically after every ingest."""
    conn, other = two_conns
    from discord_scraper import pipeline

    monkeypatch.setattr(pipeline, "catch_up", lambda *a, **k: 12)
    enqueue(conn, "c1", "catch_up")
    job = claim_next(conn)
    run_job(conn, cfg, job)
    assert worker_state(other, "last_ingest_added") == "12"
    assert worker_state(other, "last_ingest_at")
    kinds = [row[0] for row in other.execute("SELECT kind FROM jobs WHERE status = 'queued'")]
    assert kinds == ["rebuild_rollups"]


# --- the scheduler -----------------------------------------------------------

def test_schedule_is_on_by_default_and_honours_the_override(two_conns):
    conn, other = two_conns
    assert schedule_enabled(conn) is True
    set_worker_state(conn, "cron_enabled", "false")
    assert schedule_enabled(other) is False


def test_next_delay_uses_cron_hours(two_conns):
    conn, _other = two_conns
    assert next_delay_seconds(conn) == 6 * 3600
    set_worker_state(conn, "cron_hours", "1")
    assert next_delay_seconds(conn) == 3600


def _with_worker(cfg, **values):
    from dataclasses import replace

    return replace(cfg, settings=replace(cfg.settings, **values))


def test_config_toml_turns_the_schedule_off_when_the_board_never_did(two_conns, cfg, monkeypatch):
    """`[worker] cron_enabled = false` used to be read and then ignored."""
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    loop.run_forever(conn, _with_worker(cfg, cron_enabled=False), iterations=1,
                     sleep=lambda _s: None)
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_a_schedule_saved_from_the_board_beats_config_toml(two_conns, cfg):
    conn, _other = two_conns
    off = _with_worker(cfg, cron_enabled=False, cron_hours=12)
    assert schedule_enabled(conn, off) is False
    assert next_delay_seconds(conn, off) == 12 * 3600
    set_worker_state(conn, "cron_enabled", "true")
    set_worker_state(conn, "cron_hours", "2")
    assert schedule_enabled(conn, off) is True
    assert next_delay_seconds(conn, off) == 2 * 3600


def test_a_short_delay_is_never_zero(two_conns):
    conn, _other = two_conns
    set_worker_state(conn, "cron_hours", "0")
    assert next_delay_seconds(conn) >= 60


# --- the loop ----------------------------------------------------------------

def test_the_loop_drains_the_queue_and_heartbeats(two_conns, cfg):
    from discord_scraper.worker.loop import run_forever

    conn, other = two_conns
    set_worker_state(conn, "cron_enabled", "false")     # this test is about the queue
    enqueue(conn, "c1", "rebuild_rollups")
    assert run_forever(conn, cfg, iterations=1, sleep=lambda _s: None) == 1
    assert other.execute("SELECT status FROM jobs").fetchone()[0] == "done"
    assert worker_state(other, "last_seen_at")


def test_the_loop_enqueues_a_scheduled_ingest_when_due(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")    # this test is about ingest
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    run_forever = loop.run_forever
    run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs")] == ["catch_up"]


def test_a_disabled_schedule_queues_nothing(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "cron_enabled", "false")
    set_worker_state(conn, "backup_enabled", "false")
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_the_loop_survives_a_failing_job(two_conns, cfg, monkeypatch):
    """A dead token must not end ingest for the life of the process.

    `run_pending` re-raises by design; the loop is what must not die. Before the
    fix, the exception left the daemon thread and every later job — including the
    operator's manual Recalculate — sat queued until a restart.
    """
    from discord_scraper.worker import jobs as jobs_module
    from discord_scraper.worker import loop

    def boom(*a, **k):
        raise RuntimeError("HTTP 401/403: token invalid or channel not accessible")

    monkeypatch.setitem(jobs_module.DISPATCH, "catch_up", boom)
    monkeypatch.setattr(loop, "schedule_enabled", lambda conn, cfg=None: False)

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    enqueue(conn, "c1", "catch_up")
    enqueue(conn, "c1", "rebuild_rollups")

    assert loop.run_forever(conn, cfg, iterations=2, sleep=lambda _s: None) == 2

    statuses = dict(other.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status"))
    assert statuses.get("failed") == 1
    assert statuses.get("done") == 1          # the queue kept moving
    assert "401" in (worker_state(other, "last_error") or "")
    # A later success clears the counter: it counts *consecutive* failures.
    assert worker_state(other, "consecutive_failures") == "0"


def test_the_failure_counter_survives_a_pass_with_nothing_to_do(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import jobs as jobs_module
    from discord_scraper.worker import loop

    def boom(*a, **k):
        raise RuntimeError("still the same dead token")

    monkeypatch.setitem(jobs_module.DISPATCH, "catch_up", boom)
    monkeypatch.setattr(loop, "schedule_enabled", lambda conn, cfg=None: False)

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    enqueue(conn, "c1", "catch_up")
    loop.run_forever(conn, cfg, iterations=2, sleep=lambda _s: None)

    assert worker_state(other, "consecutive_failures") == "1"
    assert "dead token" in (worker_state(other, "last_error") or "")


# --- scheduling after failures ------------------------------------------------

def _failing_feed(monkeypatch, error):
    from discord_scraper import pipeline

    def fail(*a, **k):
        raise error

    monkeypatch.setattr(pipeline, "catch_up", fail)


def test_a_failed_scheduled_ingest_is_not_retried_on_the_next_pass(two_conns, cfg, monkeypatch):
    """Before the fix a failure left `last_ingest_at` alone, so every 15 s pass
    queued the same doomed scrape again."""
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    _failing_feed(monkeypatch, RuntimeError("HTTP 502 Bad Gateway"))
    loop.run_forever(conn, cfg, iterations=3, sleep=lambda _s: None)
    kinds = [row[0] for row in other.execute("SELECT kind FROM jobs")]
    assert kinds == ["catch_up"]
    assert worker_state(other, "ingest_failures") == "1"


def test_a_401_from_the_feed_marks_the_token_dead_and_pauses_the_schedule(
    two_conns, cfg, monkeypatch
):
    from discord_scraper.scraper.api import AuthError
    from discord_scraper.worker import loop
    from discord_scraper.worker.scheduler import ingest_paused
    from discord_scraper.worker.state import banner_for

    from discord_scraper.worker import probe

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    _failing_feed(monkeypatch, AuthError("HTTP 401: token is invalid or revoked", status=401))
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert worker_state(other, "token_status") == "dead"
    assert banner_for(dict(other.execute("SELECT key, value FROM worker_state"))) == "token_dead"
    # Long past the delay, a dead token is asked about, not scraped with again.
    monkeypatch.setattr(probe, "get_current_user", lambda token, timeout: (401, None, "dead"))
    set_worker_state(conn, "last_ingest_attempt_at", "2000-01-01T00:00:00+00:00")
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert ingest_paused(other)
    assert other.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'catch_up'").fetchone()[0] == 1
    assert other.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'probe'").fetchone()[0] >= 1


def test_a_paused_schedule_resumes_once_a_scheduled_probe_says_the_token_works(
    two_conns, cfg, monkeypatch
):
    from discord_scraper.worker import loop

    # A worker that has been running all along: no start-up probe to lean on.
    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    set_worker_state(conn, "token_status", "dead")
    set_worker_state(conn, "last_ingest_at", "2000-01-01T00:00:00+00:00")
    loop._queue_scheduled_work(conn, cfg)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs")] == ["probe"]
    run_job(conn, cfg, claim_next(conn), get=_responder(200))
    assert worker_state(other, "token_status") == "ok"
    # The pause is lifted, and the scrape comes on the next pass, not an interval later.
    loop._queue_scheduled_work(conn, cfg)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs ORDER BY id")] == ["probe", "catch_up"]


def test_a_network_error_does_not_bring_a_dead_token_back(two_conns):
    conn, other = two_conns
    probe_token(conn, "a-token", get=_responder(401))

    def boom(*a, **k):
        raise OSError("no route to host")

    probe_token(conn, "a-token", get=boom)
    assert worker_state(other, "token_status") == "dead"


def test_a_403_from_the_feed_leaves_the_token_alone(two_conns, cfg, monkeypatch):
    from discord_scraper.scraper.api import AuthError
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    set_worker_state(conn, "token_status", "ok")
    _failing_feed(monkeypatch, AuthError("HTTP 403: no permission", status=403))
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert worker_state(other, "token_status") == "ok"


def test_a_successful_ingest_revives_the_token_status(two_conns, cfg, monkeypatch):
    from discord_scraper import pipeline

    conn, other = two_conns
    set_worker_state(conn, "token_status", "dead")
    set_worker_state(conn, "ingest_failures", "3")
    monkeypatch.setattr(pipeline, "catch_up", lambda *a, **k: 4)
    enqueue(conn, "c1", "catch_up")
    run_job(conn, cfg, claim_next(conn))
    assert worker_state(other, "token_status") == "ok"
    assert worker_state(other, "ingest_failures") == "0"


def test_backoff_doubles_after_the_first_failure_and_caps_at_a_day(two_conns):
    from discord_scraper.worker.scheduler import delay_after_failures

    conn, _other = two_conns
    hour = 3600
    assert delay_after_failures(conn, 0) == 6 * hour
    assert delay_after_failures(conn, 1) == 6 * hour
    assert delay_after_failures(conn, 2) == 12 * hour
    assert delay_after_failures(conn, 3) == 24 * hour
    assert delay_after_failures(conn, 9) == 24 * hour


def test_jitter_stays_within_a_tenth_of_the_delay(two_conns):
    from discord_scraper.worker.scheduler import jitter_seconds

    conn, _other = two_conns
    assert jitter_seconds(conn, pick=lambda low, high: high) == 6 * 3600 // 10
    assert jitter_seconds(conn, pick=lambda low, high: low) == 0


def test_the_worker_waits_when_there_is_no_token(two_conns, cfg):
    from dataclasses import replace

    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "backup_enabled", "false")
    tokenless = replace(cfg, secrets=replace(cfg.secrets, token=""))
    loop.run_forever(conn, tokenless, iterations=1, sleep=lambda _s: None)
    # What the board shows as "token in use" when the API itself has none.
    assert worker_state(other, "token_source") == "none"
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --- start-up housekeeping ------------------------------------------------------

def test_jobs_left_running_by_a_dead_worker_are_failed_on_start(two_conns, cfg):
    from discord_scraper.rollups.build import record_event
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "cron_enabled", "false")
    set_worker_state(conn, "backup_enabled", "false")
    enqueue(conn, "c1", "rebuild_rollups")
    claim_next(conn)                                  # ...and then the worker died
    record_event(conn, source="worker", scope="rebuild", kind="run", phase="started",
                 message="rebuild started")
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    row = other.execute("SELECT status, error FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "stopped" in row["error"]
    latest = other.execute(
        "SELECT phase FROM rebuild_events WHERE kind = 'run' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert latest["phase"] == "failed"


def test_a_dead_token_is_probed_again_when_the_worker_starts(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "token_status", "dead")
    set_worker_state(conn, "backup_enabled", "false")
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs")] == ["probe"]


# --- backups -------------------------------------------------------------------

def test_a_backup_is_a_readable_copy_and_old_ones_are_pruned(two_conns, cfg):
    from pathlib import Path

    from discord_scraper import locks
    from discord_scraper.worker.jobs import _backup

    conn, other = two_conns
    set_worker_state(conn, "backup_keep", "2")
    folder = Path(locks.database_path(conn)).parent / "backups"
    folder.mkdir()
    for stamp in ("20200101T000000Z", "20200102T000000Z"):
        (folder / f"messages-{stamp}.db").write_bytes(b"")
    result = _backup(conn, cfg)
    copies = sorted(p.name for p in folder.glob("messages-*.db"))
    assert len(copies) == 2 and copies[0] == "messages-20200102T000000Z.db"
    assert not list(folder.glob("*.partial"))
    newest = sqlite3.connect(folder / copies[-1])
    try:
        assert newest.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == \
            conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    finally:
        newest.close()
    assert worker_state(other, "last_backup_at")
    assert result.startswith("backup: messages-")


def _backups(conn):
    from pathlib import Path

    from discord_scraper import locks

    folder = Path(locks.database_path(conn)).parent / "backups"
    folder.mkdir(exist_ok=True)
    return folder


def test_a_failed_backup_leaves_no_partial_copy_behind(two_conns, cfg):
    from discord_scraper.worker.jobs import _backup

    conn, other = two_conns
    folder = _backups(conn)
    (folder / "messages-20200101T000000Z.db.partial").write_bytes(b"left by a crash")

    class Failing:
        """Delegates to the real connection but fails the copy, like a full disk."""

        def __getattr__(self, name):
            return getattr(conn, name)

        def backup(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database or disk is full")

    with pytest.raises(sqlite3.OperationalError):
        _backup(Failing(), cfg)
    assert not list(folder.glob("*.partial"))
    assert worker_state(other, "last_backup_at") is None


def test_backups_of_one_database_never_prune_another(two_conns, cfg):
    from discord_scraper.worker.jobs import _backup

    conn, _other = two_conns
    folder = _backups(conn)
    neighbour = [folder / f"messages-old-2020010{day}T000000Z.db" for day in (1, 2, 3)]
    for path in neighbour:
        path.write_bytes(b"")
    set_worker_state(conn, "backup_keep", "1")
    _backup(conn, cfg)
    assert all(path.exists() for path in neighbour)
    assert len(list(folder.glob("messages-????????T??????Z.db"))) == 1


def test_a_copy_that_cannot_be_pruned_does_not_fail_the_backup(two_conns, cfg, monkeypatch):
    from pathlib import Path

    from discord_scraper.worker.jobs import _backup

    conn, other = two_conns
    folder = _backups(conn)
    stuck = folder / "messages-20200101T000000Z.db"
    stuck.write_bytes(b"")
    set_worker_state(conn, "backup_keep", "1")
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self == stuck:
            raise PermissionError("open in another program")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = _backup(conn, cfg)
    assert "could not remove messages-20200101T000000Z.db" in result
    assert worker_state(other, "last_backup_at")


def test_a_backup_is_scheduled_daily(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "cron_enabled", "false")
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    loop.run_forever(conn, cfg, iterations=2, sleep=lambda _s: None)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs")] == ["backup"]
    set_worker_state(conn, "backup_enabled", "false")
    set_worker_state(conn, "last_backup_attempt_at", "2000-01-01T00:00:00+00:00")
    loop.run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert other.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1


# --- the saved token -------------------------------------------------------------

def test_a_saved_token_is_owner_only_and_beats_the_environment(tmp_path, cfg):
    import os
    import stat

    from discord_scraper.config import current_token, save_token, token_file_for

    db = tmp_path / "output" / "messages.db"
    db.parent.mkdir()
    assert current_token(cfg, db) == cfg.token
    (db.parent / "secrets").mkdir(mode=0o755)
    path = save_token(db, "  saved-token\n")
    assert path == token_file_for(db)
    if os.name != "nt":   # Windows ignores these modes; the folder is the boundary there
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        # A folder that already existed, looser, is tightened rather than trusted.
        assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700
    assert current_token(cfg, db) == "saved-token"


def test_the_token_can_come_from_a_file(tmp_path, monkeypatch):
    from discord_scraper.config import _secrets_from

    secret = tmp_path / "token"
    secret.write_text("from-a-file\n")
    assert _secrets_from({"DISCORD_TOKEN_FILE": str(secret)}).token == "from-a-file"
    assert _secrets_from({"DISCORD_TOKEN": "env", "DISCORD_TOKEN_FILE": str(secret)}).token == "env"
