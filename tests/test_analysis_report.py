"""A report is a read-only view: an empty database is a message, not a crash.

A fresh install, or a scrape that has not landed a message yet, used to raise
``IndexError`` out of ``report.py`` while formatting the span line.
"""
from __future__ import annotations

import sqlite3

import pytest

from discord_scraper.analysis.report import report
from discord_scraper.cli import main
from discord_scraper.scraper.database import SCHEMA


def _empty_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """The schema exists but no scrape has landed a message yet."""
    path = _empty_db(tmp_path / "messages.db")
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", path)
    return path


@pytest.fixture
def missing_db(tmp_path, monkeypatch):
    """No database file at all — the state right after copying the config."""
    path = tmp_path / "missing.db"
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", path)
    return path


def test_report_on_an_empty_database_says_so(empty_db, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "report", "--out", str(tmp_path / "report")])
    assert "no messages yet" in str(exc.value)


def test_report_on_an_empty_database_writes_nothing(empty_db, tmp_path):
    out = tmp_path / "report"
    with pytest.raises(SystemExit):
        main(["analyze", "report", "--out", str(out)])
    assert not out.exists()


def test_default_report_on_an_empty_database_says_so(empty_db, tmp_path):
    """`discord-scraper analyze` with no subcommand takes the same path."""
    with pytest.raises(SystemExit) as exc:
        main(["analyze"])
    assert "no messages yet" in str(exc.value)


def test_report_function_on_an_empty_database_says_so(empty_db, tmp_path):
    """`python3 analysis.py` with no arguments ends up here, via the CLI."""
    with pytest.raises(SystemExit) as exc:
        report(out_dir=tmp_path / "report")
    assert "no messages yet" in str(exc.value)


def test_report_on_a_schemaless_database_says_so(tmp_path, monkeypatch):
    path = tmp_path / "messages.db"
    sqlite3.connect(path).close()  # a zero-byte file, e.g. created by hand
    monkeypatch.setattr("discord_scraper.analysis.db.DB_PATH", path)
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "report", "--out", str(tmp_path / "report")])
    assert "no messages yet" in str(exc.value)


def test_report_with_no_database_file_says_so(missing_db, tmp_path):
    with pytest.raises(SystemExit) as exc:
        main(["analyze", "report", "--out", str(tmp_path / "report")])
    assert "No database at" in str(exc.value)
    assert "discord-scraper scrape" in str(exc.value)