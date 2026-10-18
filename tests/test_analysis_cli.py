"""`discord-scraper analyze` is tier 1's advertised analysis interface.

The dispatch used to call the analysis ``main()`` with an argument it did not
accept, so every named subcommand died with a ``TypeError``. These tests drive
the real ``discord_scraper.cli.main`` into it.
"""
from __future__ import annotations

import importlib
import sqlite3

import pytest

from discord_scraper.cli import main
from discord_scraper.scraper.database import SCHEMA

MESSAGES = [
    ("1", "chan", "u1", "alice", "hello world 🙂", "2024-01-05T10:00:00", 0),
    ("2", "chan", "u2", "bob", "hello there", "2024-02-05T10:00:00", 0),
]


def _make_db(path, rows=MESSAGES):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executemany(
        "INSERT INTO messages (id, channel_id, author_id, author_username, content,"
        " created_at, type, raw) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        rows,
    )
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """A real messages.db with a couple of rows, wired into the analysis reader."""
    path = _make_db(tmp_path / "messages.db")
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", path)
    return path


def test_analyze_dispatches_stats(seeded_db, capsys):
    assert main(["analyze", "stats"]) == 0
    out = capsys.readouterr().out
    assert "messages" in out
    assert "alice" not in out  # no traceback, and not silently the full report


def test_analyze_forwards_the_subcommand_name(seeded_db, capsys):
    assert main(["analyze", "top-authors"]) == 0
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["stats"],
        ["activity", "--by", "month"],
        ["top-authors", "--limit", "5"],
        ["reactions"],
        ["attachments"],
        ["mentions"],
        ["most-replied"],
        ["search", "hello"],
        ["top-words", "--min-len", "4"],
        ["emoji"],
    ],
)
def test_every_named_subcommand_is_reachable(seeded_db, argv):
    assert main(["analyze", *argv]) == 0


def test_analyze_with_no_subcommand_runs_the_default_report(tmp_path, monkeypatch):
    _make_db(tmp_path / "messages.db")
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", tmp_path / "messages.db")
    # The package re-exports `report` as a function, so reach the module itself.
    report_mod = importlib.import_module("discord_scraper.analysis.report")
    monkeypatch.setattr(report_mod, "OUTPUT_DIR", tmp_path)
    assert main(["analyze"]) == 0
    assert (tmp_path / "report" / "report.txt").is_file()


def test_analyze_report_writes_where_it_is_told(tmp_path, monkeypatch):
    _make_db(tmp_path / "messages.db")
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", tmp_path / "messages.db")
    out = tmp_path / "elsewhere"
    assert main(["analyze", "report", "--out", str(out), "--bucket", "month"]) == 0
    assert (out / "report.txt").is_file()


def test_analyze_rejects_an_unknown_subcommand(seeded_db, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "nope"])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "invalid choice" in err
    assert "Traceback" not in err