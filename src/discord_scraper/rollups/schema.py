"""Derived rollups and the control plane.

One module owns the DDL so every table's shape is decided in one place: the
rollups are rebuilt wholesale, never hand-edited, and every one of them is keyed
by ``channel_id`` first even though v1 serves a single channel (spec §5.3), so
adding a second channel is a data change and not a migration.

Provenance lives in ``rollup_meta``: one row per (channel, family) recording
what the rollup was built *from*, which is what makes "is this stale?" a query
rather than a guess.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

FAMILIES: tuple[str, ...] = (
    "activity",
    "heat",
    "reply",
    "word",
    "link",
    "provider",
    "embed_type",
    "media",
    "emoji",
    "reaction",
    "attachment",
    "response",
    "starter",
    "edit_speed",
    "type",
)

ROLLUP_TABLES: tuple[str, ...] = tuple(f"rollup_{family}" for family in FAMILIES)

# The families whose rows are keyed on a person. `type` is not one of them: it
# counts message types and is derived before the identity filter, so it stays
# non-empty when every author is excluded — which is exactly why "nothing was
# written at all" is the wrong test for "every panel is blank".
AUTHOR_FAMILIES: tuple[str, ...] = (
    "activity",
    "heat",
    "reply",
    "word",
    "link",
    "provider",
    "emoji",
    "reaction",
    "attachment",
    "response",
    "starter",
    "edit_speed",
)

ROLLUP_DDL = """
CREATE TABLE IF NOT EXISTS rollup_activity (
  channel_id TEXT NOT NULL,
  day        TEXT NOT NULL,                -- local date at rollup timezone
  author_id  TEXT NOT NULL,
  msgs       INTEGER NOT NULL,
  chars      INTEGER NOT NULL,
  media      INTEGER NOT NULL DEFAULT 0,   -- attachment OR embed
  edits      INTEGER NOT NULL DEFAULT 0,
  replies    INTEGER NOT NULL DEFAULT 0,   -- messages of type 19
  PRIMARY KEY (channel_id, day, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_heat (
  channel_id TEXT NOT NULL,
  year       INTEGER NOT NULL,
  dow        INTEGER NOT NULL,             -- 0=Sun, local
  hour       INTEGER NOT NULL,             -- 0-23, local
  author_id  TEXT NOT NULL,
  msgs       INTEGER NOT NULL,
  PRIMARY KEY (channel_id, year, dow, hour, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_reply (
  channel_id       TEXT NOT NULL,
  parent_author_id TEXT NOT NULL,
  child_author_id  TEXT NOT NULL,
  msgs             INTEGER NOT NULL,
  PRIMARY KEY (channel_id, parent_author_id, child_author_id)
);

CREATE TABLE IF NOT EXISTS rollup_word (
  channel_id TEXT NOT NULL,
  word       TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  uses       INTEGER NOT NULL,
  PRIMARY KEY (channel_id, word, author_id)
);
CREATE INDEX IF NOT EXISTS idx_rollup_word ON rollup_word (word);

CREATE TABLE IF NOT EXISTS rollup_link (
  channel_id TEXT NOT NULL,
  day        TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  domain     TEXT NOT NULL,                -- top-N domains only
  uses       INTEGER NOT NULL,
  PRIMARY KEY (channel_id, day, author_id, domain)
);

CREATE TABLE IF NOT EXISTS rollup_provider (
  channel_id TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  provider   TEXT NOT NULL,
  embeds     INTEGER NOT NULL,
  PRIMARY KEY (channel_id, author_id, provider)
);

CREATE TABLE IF NOT EXISTS rollup_embed_type (
  channel_id TEXT NOT NULL,
  embed_type TEXT NOT NULL,
  embeds     INTEGER NOT NULL,
  PRIMARY KEY (channel_id, embed_type)
);

-- One row per distinct embed URL. `rollup_provider` counts embeds per author;
-- this counts the media itself, which is what "reposted 28 times" needs. The
-- provider and kind are whatever Discord reports, so a switch from Tenor to
-- Klipy needs no code change.
CREATE TABLE IF NOT EXISTS rollup_media (
    channel_id TEXT NOT NULL,
    url        TEXT NOT NULL,
    provider   TEXT NOT NULL,
    kind       TEXT NOT NULL,
    title      TEXT,
    thumbnail  TEXT,
    -- The playable version when Discord offers one: an mp4 for a gifv, which
    -- animates and is usually far smaller than the GIF itself. The thumbnail is
    -- often only a static poster, which is why cards used to sit still.
    animated   TEXT,
    uses       INTEGER NOT NULL,
    first_seen TEXT,
    last_seen  TEXT,
    PRIMARY KEY (channel_id, url)
);

CREATE TABLE IF NOT EXISTS rollup_emoji (
  channel_id TEXT NOT NULL,
  emoji      TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  uses       INTEGER NOT NULL,
  PRIMARY KEY (channel_id, emoji, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_reaction (
  channel_id TEXT NOT NULL,
  emoji      TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  count      INTEGER NOT NULL,
  PRIMARY KEY (channel_id, emoji, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_attachment (
  channel_id TEXT NOT NULL,
  mime       TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  count      INTEGER NOT NULL,
  PRIMARY KEY (channel_id, mime, author_id)
);

-- Replies within 0 < dt <= 86400s only; longer gaps are not a response time.
-- capped_replies counts the pairs the window excluded, so an average built on
-- this table can say how much it threw away.
CREATE TABLE IF NOT EXISTS rollup_response (
  channel_id     TEXT NOT NULL,
  author_id      TEXT NOT NULL,
  replies        INTEGER NOT NULL,
  total_seconds  INTEGER NOT NULL,
  capped_replies INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_id, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_starter (
  channel_id TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  starters   INTEGER NOT NULL,
  PRIMARY KEY (channel_id, author_id)
);

-- Same shape as response: a rate lives in rollup_activity, the latency here.
CREATE TABLE IF NOT EXISTS rollup_edit_speed (
  channel_id    TEXT NOT NULL,
  author_id     TEXT NOT NULL,
  edits         INTEGER NOT NULL,
  total_seconds INTEGER NOT NULL,
  capped_edits  INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (channel_id, author_id)
);

CREATE TABLE IF NOT EXISTS rollup_type (
  channel_id TEXT NOT NULL,
  type       INTEGER NOT NULL,
  msgs       INTEGER NOT NULL,
  PRIMARY KEY (channel_id, type)
);

-- Totals the capped grains cannot answer: how many messages carry a link, how
-- many distinct hosts exist before the top-60 cut, and how many messages carry
-- both a link and an embed (spec §5.11 measures all three).
CREATE TABLE IF NOT EXISTS rollup_stats (
  channel_id TEXT NOT NULL,
  key        TEXT NOT NULL,
  value      INTEGER NOT NULL,
  PRIMARY KEY (channel_id, key)
);

CREATE TABLE IF NOT EXISTS rollup_meta (
  channel_id            TEXT NOT NULL,
  family                TEXT NOT NULL,
  computed_at           TEXT NOT NULL,
  source_max_message_id TEXT NOT NULL,
  source_count          INTEGER NOT NULL,
  timezone              TEXT NOT NULL,
  config_hash           TEXT NOT NULL,     -- hash of the stopword/ignore inputs
  PRIMARY KEY (channel_id, family)
);
"""

CONTROL_TABLES: tuple[str, ...] = (
    "jobs",
    "author_names",
    "sessions",
    "viewers",
    "login_attempts",
    "config",
    "worker_state",
    "identity",
)

CONTROL_DDL = """
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  status TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  result TEXT,
  error TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,                     -- random, stored hashed
  discord_id TEXT NOT NULL,
  level TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS viewers (
  discord_id TEXT PRIMARY KEY,
  note TEXT,
  allow INTEGER NOT NULL,
  added_at TEXT
);

CREATE TABLE IF NOT EXISTS login_attempts (
  ip TEXT NOT NULL,
  at TEXT NOT NULL,
  ok INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS config (
  key TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS worker_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- Display names, so the API can name an author without scanning `messages`
-- (spec §5.3) and so an admin rename survives a rebuild (§5.5). `renamed` marks
-- a human's choice, which a rebuild must not overwrite.
CREATE TABLE IF NOT EXISTS author_names (
  channel_id TEXT NOT NULL,
  author_id  TEXT NOT NULL,
  name       TEXT NOT NULL,
  renamed    INTEGER NOT NULL DEFAULT 0,
  -- "Who shows" (§5.6): excluded identities are removed at build time, exactly
  -- like a merge, so the stats page cannot undo the decision client-side and no
  -- message is ever deleted.
  included   INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (channel_id, author_id)
);

-- Merging happens before aggregation, so no rollup needs to know identity exists.
CREATE TABLE IF NOT EXISTS identity (
  channel_id   TEXT NOT NULL,
  canonical_id TEXT NOT NULL,
  account_id   TEXT NOT NULL,
  note         TEXT,
  PRIMARY KEY (channel_id, account_id)
);
"""


def create_rollup_tables(conn: sqlite3.Connection) -> None:
    """Create every derived table. Idempotent."""
    conn.executescript(ROLLUP_DDL)
    migrate(conn)


def create_control_tables(conn: sqlite3.Connection) -> None:
    """Create the control plane (auth, jobs, worker health, identity). Idempotent."""
    conn.executescript(CONTROL_DDL)
    migrate(conn)


def migrate(conn: sqlite3.Connection) -> None:
    """Bring an existing database up to the current shape.

    ``CREATE TABLE IF NOT EXISTS`` never adds a column, so a database created
    before a column existed keeps the old shape — and the first write that names
    it fails. That happened twice: §5.6's ``included`` flag ("no column named
    included" on a running instance) and ``rollup_media.animated``. Column
    additions belong here.
    """
    added = (
        ("author_names", "included",
         "ALTER TABLE author_names ADD COLUMN included INTEGER NOT NULL DEFAULT 1"),
        ("rollup_media", "animated",
         "ALTER TABLE rollup_media ADD COLUMN animated TEXT"),
    )
    for table, column, ddl in added:
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if columns and column not in columns:
            conn.execute(ddl)
    conn.commit()


def _rows_as_dicts(cursor: sqlite3.Cursor, rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Mapping results without requiring ``conn.row_factory`` to be set."""
    columns = [description[0] for description in cursor.description or ()]
    return [dict(zip(columns, row)) for row in rows]


def provenance(conn: sqlite3.Connection, channel_id: str, family: str) -> dict[str, Any] | None:
    """The recorded build inputs for one family, or ``None`` if never built."""
    cursor = conn.execute(
        """
        SELECT channel_id, family, computed_at, source_max_message_id,
               source_count, timezone, config_hash
          FROM rollup_meta
         WHERE channel_id = ? AND family = ?
        """,
        (channel_id, family),
    )
    found = _rows_as_dicts(cursor, cursor.fetchall())
    return found[0] if found else None


def set_provenance(
    conn: sqlite3.Connection,
    channel_id: str,
    family: str,
    *,
    computed_at: str,
    source_max_id: str,
    source_count: int,
    timezone: str,
    config_hash: str,
) -> None:
    """Record what this family was built from.

    The caller owns the transaction: a full rebuild records provenance for all
    fourteen families inside the one transaction that wrote their rows, so a
    failure half way cannot leave provenance claiming work that rolled back.
    """
    conn.execute(
        """
        INSERT INTO rollup_meta (channel_id, family, computed_at, source_max_message_id,
                                 source_count, timezone, config_hash)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (channel_id, family) DO UPDATE SET
            computed_at = excluded.computed_at,
            source_max_message_id = excluded.source_max_message_id,
            source_count = excluded.source_count,
            timezone = excluded.timezone,
            config_hash = excluded.config_hash
        """,
        (channel_id, family, computed_at, source_max_id, source_count, timezone, config_hash),
    )


def is_stale(
    conn: sqlite3.Connection,
    channel_id: str,
    family: str,
    *,
    config_hash: str | None = None,
    cheap: bool = False,
) -> bool:
    """True when the source or the word-list inputs no longer match the build.

    Count and newest id together catch both a new message and a deletion; either
    alone misses one of them. ``config_hash`` is optional because it costs a
    stopword-file read to compute — callers that have it (the CLI's ``status``)
    also catch "the stopword list changed, so ``rollup_word`` is out of date".
    """
    recorded = provenance(conn, channel_id, family)
    if recorded is None:
        return True
    if config_hash is not None and recorded["config_hash"] != config_hash:
        return True
    if cheap:
        # One indexed lookup (~0 ms on 492k rows). The full comparison below costs
        # a 766 MB scan, which is fine for `rollups status` and unacceptable on
        # every dashboard load. The trade-off is explicit: this detects new
        # messages, not deletions — and the scraper never deletes.
        #
        # It compares ids as strings, which is chronological because Discord
        # snowflakes are fixed-width. Fixtures with hand-written ids ("9" vs "10")
        # violate that, which is why the tests here use padded ids.
        # The primary-key index scanned backwards finds the newest id without
        # looking at any other row: 0 ms against 2.4 s for MAX(id) once a channel
        # index exists, because the planner then prefers the channel index.
        try:
            newest = conn.execute(
                "SELECT id FROM messages INDEXED BY sqlite_autoindex_messages_1 "
                " WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                (channel_id,),
            ).fetchone()
        except sqlite3.OperationalError:
            # The implicit index has a generated name. If a schema ever changes it,
            # this path is still correct — just slower — and a missing table reads
            # as stale either way.
            try:
                newest = conn.execute(
                    "SELECT id FROM messages WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
                    (channel_id,),
                ).fetchone()
            except sqlite3.OperationalError:
                return True
        return (recorded["source_max_message_id"] or "") != ((newest[0] if newest else "") or "")

    try:
        current_count, current_max = conn.execute(
            "SELECT COUNT(*), MAX(id) FROM messages WHERE channel_id = ?", (channel_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        # No messages table at all: nothing can be current, and guessing
        # "current" is the failure this flag exists to prevent.
        return True
    return (
        int(recorded["source_count"]) != int(current_count or 0)
        or (recorded["source_max_message_id"] or "") != (current_max or "")
    )
