"""Operator overrides: the control-plane ``config`` table, and nothing else.

This is the single authority for the difference between the file and what the
operator asked for. The API, the worker and the CLI all resolve configuration
through it, so a rebuild bakes the same zone the dashboard compares against —
the two must never disagree about which settings are in force.

The key set is fixed on purpose: an admin endpoint that writes arbitrary rows is
a liability, not a feature. ``OVERRIDABLE`` is the whole vocabulary an operator
may change without editing the file; anything else in the table is ignored on
read and refused on write.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from discord_scraper.config import (
    ALLOWLIST_MODES,
    MAX_CRON_HOURS,
    MIN_CRON_HOURS,
    Config,
    is_timezone,
)

# The control-plane `config` table holds operator overrides. A fixed key set:
# an admin endpoint that writes arbitrary rows is a liability, not a feature.
OVERRIDABLE = ("channel_id", "timezone", "ignore_words", "allowlist_policy",
               "cron_enabled", "cron_hours")


class InvalidOverride(ValueError):
    """A value that would be stored and then break the next rebuild."""


def read(conn: sqlite3.Connection) -> dict[str, str]:
    """Every stored override, as raw strings.

    An older database may have no ``config`` table yet — the worker can run
    before the API has migrated it — and "no table" is simply "no overrides".
    """
    try:
        return dict(conn.execute("SELECT key, value FROM config"))
    except sqlite3.OperationalError:
        return {}


def validate(key: str, value: Any) -> None:
    """Refuse a value the loader would refuse, before it is written.

    The save is the cheapest place to catch a typo: every later rebuild reads it
    back, and a bad stopword list or zone takes that rebuild down instead.
    """
    if key == "timezone":
        if not is_timezone(str(value)):
            raise InvalidOverride(
                f'unknown timezone: {value} (use an IANA name like '
                '"Europe/London", or "auto")',
            )
    elif key == "allowlist_policy" and str(value) not in ALLOWLIST_MODES:
        raise InvalidOverride(
            f"allowlist_policy must be one of {', '.join(ALLOWLIST_MODES)}",
        )
    elif key == "ignore_words":
        # The loader refuses a stopword list that is not words for the same
        # reason: `text.py` calls `.strip()` on every entry, so anything else
        # takes the next rebuild down instead of the save that caused it.
        if not isinstance(value, list) or not all(isinstance(word, str) for word in value):
            raise InvalidOverride("ignore_words must be a list of words")
    elif key == "channel_id":
        # A list stringifies to "['a']" and reads as non-empty, which is how a
        # wrong type used to reach the channel the whole dashboard queries.
        if not isinstance(value, str) or not value.strip():
            raise InvalidOverride("channel_id must be a channel id")
    elif key == "cron_hours":
        try:
            hours = int(value)
        except (TypeError, ValueError):
            raise InvalidOverride("cron_hours must be a number") from None
        if not MIN_CRON_HOURS <= hours <= MAX_CRON_HOURS:
            raise InvalidOverride("cron_hours must be between 1 and 24")


def apply(base: Config, overrides: dict[str, str]) -> Config:
    """``base`` with the operator's overrides applied.

    Unknown keys are ignored rather than refused: a stale row left by an older
    build must not stop the worker. The result is a new ``Config`` — the base,
    secrets included, is never mutated.
    """
    changes: dict[str, Any] = {}
    if "channel_id" in overrides:
        changes["channel_id"] = overrides["channel_id"]
    if "timezone" in overrides:
        changes["timezone"] = overrides["timezone"]
    if "allowlist_policy" in overrides:
        changes["allowlist"] = overrides["allowlist_policy"]
    if "ignore_words" in overrides:
        try:
            changes["ignore_words"] = json.loads(overrides["ignore_words"])
        except ValueError:
            changes["ignore_words"] = []
    if "cron_enabled" in overrides:
        changes["cron_enabled"] = str(overrides["cron_enabled"]).lower() in {"1", "true", "yes", "on"}
    if "cron_hours" in overrides:
        try:
            changes["cron_hours"] = int(overrides["cron_hours"])
        except ValueError:
            pass
    if not changes:
        return base
    return replace(base, settings=replace(base.settings, **changes))


def effective_for(conn: sqlite3.Connection, base: Config) -> Config:
    """``base`` with the stored operator overrides — the one call sites use."""
    return apply(base, read(conn))
