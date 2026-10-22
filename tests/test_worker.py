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
    monkeypatch.setitem(loop.__dict__, "run_pending", lambda conn, cfg: 0)
    run_forever = loop.run_forever
    run_forever(conn, cfg, iterations=1, sleep=lambda _s: None)
    assert [row[0] for row in other.execute("SELECT kind FROM jobs")] == ["catch_up"]


def test_a_disabled_schedule_queues_nothing(two_conns, cfg, monkeypatch):
    from discord_scraper.worker import loop

    conn, other = two_conns
    set_worker_state(conn, "cron_enabled", "false")
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
    monkeypatch.setattr(loop, "schedule_enabled", lambda conn: False)

    conn, other = two_conns
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
    monkeypatch.setattr(loop, "schedule_enabled", lambda conn: False)

    conn, other = two_conns
    enqueue(conn, "c1", "catch_up")
    loop.run_forever(conn, cfg, iterations=2, sleep=lambda _s: None)

    assert worker_state(other, "consecutive_failures") == "1"
    assert "dead token" in (worker_state(other, "last_error") or "")
