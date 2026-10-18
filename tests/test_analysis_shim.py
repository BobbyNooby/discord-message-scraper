"""`python3 analysis.py <cmd>` must behave like `discord-scraper analyze <cmd>`.

The root shim ignored ``sys.argv`` and always called the default report, so the
documented-looking ``python3 analysis.py stats`` silently did something else.
"""

from __future__ import annotations

import importlib
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

from discord_scraper.scraper.database import SCHEMA

SHIM = Path(__file__).resolve().parents[1] / "analysis.py"

MESSAGES = [
    ("1", "chan", "u1", "alice", "hello world 🙂", "2024-01-05T10:00:00", 0),
    ("2", "chan", "u2", "bob", "hello there", "2024-02-05T10:00:00", 0),
]


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO messages (id, channel_id, author_id, author_username, content,"
        " created_at, type, raw) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        MESSAGES,
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", path)
    # The default report writes to OUTPUT_DIR/report; keep that in tmp_path so a
    # stray run cannot drop charts into the checkout.
    report_mod = importlib.import_module("discord_scraper.analysis.report")
    monkeypatch.setattr(report_mod, "OUTPUT_DIR", tmp_path)
    return path


def _run_shim(argv, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["analysis.py", *argv])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(SHIM), run_name="__main__")
    return exc.value.code


def test_the_shim_forwards_a_named_subcommand(seeded_db, monkeypatch, capsys):
    """`stats` must print the stats table, not the default report."""
    assert _run_shim(["stats"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "messages" in out
    assert "alice" not in out  # the report's per-user dump, i.e. the wrong command


def test_the_shim_forwards_options_to_the_subcommand(seeded_db, monkeypatch, capsys):
    assert _run_shim(["search", "hello there"], monkeypatch) == 0
    out = capsys.readouterr().out
    assert "hello there" in out
    assert "hello world" not in out


def test_the_shim_with_no_arguments_still_runs_the_default_report(
    seeded_db, tmp_path, monkeypatch
):
    assert _run_shim([], monkeypatch) == 0
    assert (tmp_path / "report" / "report.txt").is_file()
