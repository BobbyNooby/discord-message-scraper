"""Catch-up resume behaviour, against a temp DB and a fake API.

The HTTP boundary (`fetch_messages`) is the only thing faked; the SQLite
layer is real. Run with:

    python3 -m pytest tests/
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from discord_scraper import pipeline as main
from discord_scraper.scraper import database
from discord_scraper.scraper.config import Config


def message(i: int) -> dict:
    """Minimal API message object, shaped for scraper.transform.to_row."""
    return {
        "id": str(i),
        "channel_id": "1",
        "author": {"id": "u1", "username": "bob"},
        "content": f"msg {i}",
        "timestamp": "2026-01-01T00:00:00+00:00",
        "type": 0,
        "pinned": False,
    }


class FakeChannel:
    """Stands in for the Discord API: newest-first pages, `before` respected."""

    def __init__(self, ids):
        self.ascending = [message(i) for i in sorted(ids)]
        self.calls = 0

    def fetch(self, token, channel_id, before=None, limit=50):
        self.calls += 1
        pool = self.ascending
        if before is not None:
            pool = [m for m in pool if int(m["id"]) < int(before)]
        return list(reversed(pool[-limit:]))


class CatchUpTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db_path = Path(tmp.name) / "test.db"

        for target, name, value in (
            (database, "DB_PATH", db_path),
            (main, "REQUEST_DELAY_S", 0),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.conn = database.open_db()
        self.addCleanup(self.conn.close)
        self.cfg = Config(token="token", channel_id="1", batch_size=50)

    def seed(self, ids):
        database.insert_batch(self.conn, [message(i) for i in ids])
        database.set_meta(self.conn, "oldest_id", str(min(ids)))

    def stored_ids(self):
        return {int(r[0]) for r in self.conn.execute("SELECT id FROM messages")}

    def run_catch_up(self, fetch, max_batches=None):
        with mock.patch.object(main, "fetch_messages", fetch), contextlib.redirect_stdout(
            io.StringIO()
        ):
            return main.catch_up(
                self.conn, self.cfg, main.Budget(max_batches), time.monotonic()
            )

    def test_interrupted_catch_up_resumes_and_fills_the_hole(self):
        """The whole point: a run killed mid-catch-up must not leave a hole
        that later runs skip over."""
        self.seed(range(1, 101))
        channel = FakeChannel(range(1, 201))

        # A pass that dies after one batch, having written the 151-200 stripe.
        calls = {"n": 0}

        def flaky(token, channel_id, before=None, limit=50):
            calls["n"] += 1
            if calls["n"] > 1:
                raise KeyboardInterrupt("killed mid catch-up")
            return channel.fetch(token, channel_id, before, limit)

        with self.assertRaises(KeyboardInterrupt):
            self.run_catch_up(flaky)
        self.assertEqual(self.stored_ids(), set(range(1, 101)) | set(range(151, 201)))

        # The re-run hits the 151-200 stripe straight away; it must keep going.
        self.run_catch_up(channel.fetch)

        self.assertEqual(self.stored_ids(), set(range(1, 201)))
        self.assertIsNone(database.get_meta(self.conn, "catchup_cursor"))

    def test_already_up_to_date_costs_a_single_request(self):
        """Guards the fast path: being current must not trigger a re-walk."""
        self.seed(range(1, 201))
        channel = FakeChannel(range(1, 201))

        self.run_catch_up(channel.fetch)

        self.assertEqual(channel.calls, 1)
        self.assertIsNone(database.get_meta(self.conn, "catchup_cursor"))

    def test_partial_pass_keeps_the_cursor_for_the_next_run(self):
        self.seed(range(1, 101))
        channel = FakeChannel(range(1, 201))

        self.run_catch_up(channel.fetch, max_batches=1)
        self.assertEqual(database.get_meta(self.conn, "catchup_cursor"), "151")

        self.run_catch_up(channel.fetch)
        self.assertEqual(self.stored_ids(), set(range(1, 201)))
        self.assertIsNone(database.get_meta(self.conn, "catchup_cursor"))

    def test_new_messages_above_a_hole_do_not_erase_the_resume_point(self):
        """Messages arriving while a hole is open sit above it; fetching them
        must not overwrite the recorded bottom of the hole."""
        self.seed(list(range(1, 101)) + list(range(151, 201)))
        database.set_meta(self.conn, "catchup_cursor", "151")
        channel = FakeChannel(range(1, 251))

        self.run_catch_up(channel.fetch)

        self.assertEqual(self.stored_ids(), set(range(1, 251)))
        self.assertIsNone(database.get_meta(self.conn, "catchup_cursor"))


if __name__ == "__main__":
    unittest.main()
