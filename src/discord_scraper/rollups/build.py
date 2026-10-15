"""Build every rollup from ``messages``.

Two rules shape this module:

* **Delete, then insert — never incremental.** A rebuild is the only way rows
  change, so a partially-updated rollup is impossible by construction.
* **One transaction for the whole rebuild.** Fourteen families and their
  provenance either all land or none do; anything else leaves provenance
  claiming work that rolled back.

The per-family functions are independently callable (Task 8's CLI exposes them
through ``rollups rebuild``), which costs one scan of ``messages`` per family.
That is a batch cost, paid by a background job, never by a request.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from discord_scraper.config import Config
from discord_scraper.rollups.identity import Identities, counted_scope, latest_display_names, resolve_identities
from discord_scraper.rollups.schema import (
    FAMILIES,
    create_control_tables,
    create_rollup_tables,
    set_provenance,
)
from discord_scraper.text import emoji, links, load_stopwords, tokens
from discord_scraper.timeutil import local_parts

# Reply latency and edit latency share one window. Beyond a day it is not a
# "response" any more, and counting it would let one message from last year
# dominate an average (spec §5.2, §5.12).
LATENCY_CAP_SECONDS = 86_400

# Only the busiest hosts ship a per-day cell; the long tail is dropped from the
# interactive panels (spec §5.11).
TOP_DOMAINS = 60

_UNKNOWN = "unknown"
_UNPROVIDENCED = "unprovidenced"

_COLUMNS = (
    "id", "author_id", "content", "created_at", "edited_at", "reply_to_id",
    "has_attachments", "type", "raw",
)


def _messages(conn: sqlite3.Connection, cfg: Config, *, all_types: bool = False) -> list[dict]:
    """The counted messages, oldest first, as plain dicts.

    Dicts rather than ``sqlite3.Row``: this module must not care whether its
    caller set a row factory — the CLI, the worker and the tests each open their
    own connection, and a rebuild that only works under one of them is a trap.

    ``all_types`` ignores ``source.count_types`` and the bot rule — only the
    message-type breakdown wants every row, because a type distribution that
    shows two of Discord's dozen types answers nothing.
    """
    where, params = counted_scope(cfg) if not all_types else ("channel_id = ?", [cfg.channel_id])
    found = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM messages WHERE {where} ORDER BY created_at, id",
        params,
    ).fetchall()
    return [dict(zip(_COLUMNS, row)) for row in found]


def _table_for(family: str) -> str:
    return f"rollup_{family}"


def _write(conn: sqlite3.Connection, channel_id: str, family: str, columns: str,
           rows: Iterable[tuple]) -> int:
    """Replace one family's rows for this channel. Returns the row count."""
    rows = list(rows)
    conn.execute(f"DELETE FROM {_table_for(family)} WHERE channel_id = ?", (channel_id,))
    if rows:
        placeholders = ", ".join("?" for _ in range(len(columns.split(","))))
        conn.executemany(
            f"INSERT INTO {_table_for(family)} ({columns}) VALUES ({placeholders})", rows
        )
    return len(rows)


def _author_key(ids: Identities, author_id: str) -> str | None:
    """The id a rollup row is filed under: the merged canonical account.

    Rollups store ids, not array positions: `rollup_activity.author_id` says
    `author_id` (spec §5.2), the index is a payload detail rebuilt by Task 9, and
    an index would silently rebind if the author list ever reordered.
    """
    return ids.canonical.get(author_id)


def _raw_field(raw: str | None, field: str) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        value = json.loads(raw).get(field)
    except (ValueError, AttributeError):
        return []
    return value if isinstance(value, list) else []


def _has_media(row: dict) -> bool:
    """Attachment OR embed (spec §5.10).

    The ``json_type`` guard is not optional: ``json_array_length`` raises on a
    non-array, and ``embeds`` is absent or null on messages that have none.
    """
    if row["has_attachments"]:
        return True
    try:
        embeds = json.loads(row["raw"] or "{}").get("embeds")
    except (ValueError, AttributeError):
        return False
    return isinstance(embeds, list) and len(embeds) > 0


def _seconds_between(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    try:
        return int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return None


def normalise_link(url: str) -> tuple[str, str] | None:
    """``(host, key)`` for a URL, or ``None`` if it has no host.

    Query and fragment are stripped so YouTube's tracking parameters and
    ``?si=`` do not shred one link into five entries (spec §5.11). The key keeps
    the path because "Most reposted links" is a panel about *links*, while the
    host is what the top-60 cut ranks.
    """
    candidate = url if "//" in url else f"//{url}"
    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None
    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return host, f"{host}{path}"


def _name_tokens(conn: sqlite3.Connection) -> set[str]:
    """Tokens that are somebody's handle or display name (spec §6.2 always-on 3)."""
    names = set(latest_display_names(conn).values())
    names |= {row[0] for row in conn.execute(
        "SELECT DISTINCT author_username FROM messages WHERE author_username IS NOT NULL"
    )}
    found: set[str] = set()
    for name in names:
        found.update(tokens(str(name)))
    return found


def config_digest(cfg: Config) -> str:
    """Fingerprint of the word-list inputs, so a config edit is detectable.

    Public because the CLI's `status` compares it: a stopword edit changes what
    `rollup_word` should contain without changing a single source row, so the
    message fingerprint alone would report "current" (§5.2 stores this to make
    that comparison possible).
    """
    parts = [",".join(sorted(cfg.stopword_packs)), ",".join(sorted(cfg.ignore_words))]
    parts.append(",".join(sorted(load_stopwords(cfg))))
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


# --- families ----------------------------------------------------------------

def rebuild_type(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    counts = conn.execute(
        "SELECT type, COUNT(*) FROM messages WHERE channel_id = ? GROUP BY type", (cfg.channel_id,)
    ).fetchall()
    return _write(conn, cfg.channel_id, "type", "channel_id, type, msgs",
                  [(cfg.channel_id, int(t if t is not None else -1), n) for t, n in counts])


def rebuild_activity(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: dict[tuple[str, int], list[int]] = {}
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        day = local_parts(row["created_at"], cfg.timezone)[0]
        cell = cells.setdefault((day, author), [0, 0, 0, 0, 0])
        cell[0] += 1
        cell[1] += len(row["content"] or "")
        cell[2] += 1 if _has_media(row) else 0
        cell[3] += 1 if row["edited_at"] else 0
        cell[4] += 1 if row["type"] == 19 else 0
    return _write(conn, cfg.channel_id, "activity", "channel_id, day, author_id, msgs, chars, media, edits, replies",
                  [(cfg.channel_id, day, author, *counts) for (day, author), counts in cells.items()])


def rebuild_heat(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[tuple[int, int, int, int]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        _, year, weekday, hour = local_parts(row["created_at"], cfg.timezone)
        cells[(year, (weekday + 1) % 7, hour, author)] += 1   # store 0=Sun, like the spec
    return _write(conn, cfg.channel_id, "heat", "channel_id, year, dow, hour, author_id, msgs",
                  [(cfg.channel_id, year, dow, hour, author, n)
                   for (year, dow, hour, author), n in cells.items()])


def rebuild_reply(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    parents = _parent_index(conn, cfg)
    edges: Counter[tuple[int, int]] = Counter()
    for row in _messages(conn, cfg):
        parent = parents.get(row["reply_to_id"]) if row["reply_to_id"] else None
        if parent is None:
            continue
        parent_row = _author_key(ids, parent[0])
        child_row = _author_key(ids, row["author_id"])
        if parent_row is None or child_row is None:
            continue
        edges[(parent_row, child_row)] += 1
    return _write(conn, cfg.channel_id, "reply", "channel_id, parent_author_id, child_author_id, msgs",
                  [(cfg.channel_id, p, c, n) for (p, c), n in edges.items()])


def rebuild_word(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    stop = load_stopwords(cfg) | _name_tokens(conn)
    words: Counter[tuple[str, int]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        for word in tokens(row["content"] or ""):
            if word not in stop:
                words[(word, author)] += 1
    return _write(conn, cfg.channel_id, "word", "channel_id, word, author_id, uses",
                  [(cfg.channel_id, word, author, n) for (word, author), n in words.items()])


def rebuild_link(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    per_host: Counter[str] = Counter()
    found: list[tuple[str, int, str]] = []          # (day, author row, key)
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        day = local_parts(row["created_at"], cfg.timezone)[0]
        for url in links(row["content"] or ""):
            parsed = normalise_link(url)
            if parsed is None:
                continue
            host, key = parsed
            per_host[host] += 1
            found.append((day, author, key))
    keep = {host for host, _ in per_host.most_common(TOP_DOMAINS)}
    cells: Counter[tuple[str, int, str]] = Counter()
    for day, author, key in found:
        host = key.split("/", 1)[0]
        if host in keep:
            cells[(day, author, key)] += 1
    return _write(conn, cfg.channel_id, "link", "channel_id, day, author_id, domain, uses",
                  [(cfg.channel_id, day, author, key, n)
                   for (day, author, key), n in cells.items()])


def rebuild_provider(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[tuple[int, str]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        for embed in _raw_field(row["raw"], "embeds"):
            provider = ((embed or {}).get("provider") or {}).get("name") or _UNPROVIDENCED
            cells[(author, str(provider))] += 1
    return _write(conn, cfg.channel_id, "provider", "channel_id, author_id, provider, embeds",
                  [(cfg.channel_id, author, provider, n)
                   for (author, provider), n in cells.items()])


def rebuild_embed_type(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[str] = Counter()
    for row in _messages(conn, cfg):
        for embed in _raw_field(row["raw"], "embeds"):
            cells[str((embed or {}).get("type") or _UNKNOWN)] += 1
    return _write(conn, cfg.channel_id, "embed_type", "channel_id, embed_type, embeds",
                  [(cfg.channel_id, kind, n) for kind, n in cells.items()])


def rebuild_emoji(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[tuple[str, int]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        for symbol in emoji(row["content"] or ""):
            cells[(symbol, author)] += 1
    return _write(conn, cfg.channel_id, "emoji", "channel_id, emoji, author_id, uses",
                  [(cfg.channel_id, symbol, author, n)
                   for (symbol, author), n in cells.items()])


def _reaction_name(value: Any) -> str:
    """Reactions key on an emoji object: custom ones have a name, unicode a char."""
    if not isinstance(value, dict):
        return str(value)
    return str(value.get("name") or value.get("id") or _UNKNOWN)


def rebuild_reaction(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[tuple[str, int]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        for reaction in _raw_field(row["raw"], "reactions"):
            if not isinstance(reaction, dict):
                continue
            name = _reaction_name(reaction.get("emoji"))
            cells[(name, author)] += int(reaction.get("count") or 0)
    return _write(conn, cfg.channel_id, "reaction", "channel_id, emoji, author_id, count",
                  [(cfg.channel_id, name, author, n) for (name, author), n in cells.items()])


def rebuild_attachment(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[tuple[str, int]] = Counter()
    for row in _messages(conn, cfg):
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        for attachment in _raw_field(row["raw"], "attachments"):
            mime = str((attachment or {}).get("content_type") or _UNKNOWN)
            cells[(mime, author)] += 1
    return _write(conn, cfg.channel_id, "attachment", "channel_id, mime, author_id, count",
                  [(cfg.channel_id, mime, author, n) for (mime, author), n in cells.items()])


def rebuild_response(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    parents = _parent_index(conn, cfg)
    per_author: dict[int, list[int]] = {}
    for row in _messages(conn, cfg):
        parent = parents.get(row["reply_to_id"]) if row["reply_to_id"] else None
        author = _author_key(ids, row["author_id"])
        if parent is None or author is None:
            continue
        gap = _seconds_between(parent[1], row["created_at"])
        if gap is None or gap <= 0:
            continue
        bucket = per_author.setdefault(author, [0, 0, 0])
        if gap <= LATENCY_CAP_SECONDS:
            bucket[0] += 1
            bucket[1] += gap
        else:
            bucket[2] += 1
    return _write(conn, cfg.channel_id, "response", "channel_id, author_id, replies, total_seconds, capped_replies",
                  [(cfg.channel_id, author, r, s, capped)
                   for author, (r, s, capped) in per_author.items()])


def rebuild_starter(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    cells: Counter[int] = Counter()
    seen: set[str] = set()
    for row in _messages(conn, cfg):
        day = local_parts(row["created_at"], cfg.timezone)[0]
        if day in seen:
            continue
        seen.add(day)
        author = _author_key(ids, row["author_id"])
        if author is not None:
            cells[author] += 1
    return _write(conn, cfg.channel_id, "starter", "channel_id, author_id, starters",
                  [(cfg.channel_id, author, n) for author, n in cells.items()])


def rebuild_edit_speed(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> int:
    per_author: dict[int, list[int]] = {}
    for row in _messages(conn, cfg):
        if not row["edited_at"]:
            continue
        author = _author_key(ids, row["author_id"])
        if author is None:
            continue
        gap = _seconds_between(row["created_at"], row["edited_at"])
        if gap is None or gap < 0:
            continue
        bucket = per_author.setdefault(author, [0, 0, 0])
        if gap <= LATENCY_CAP_SECONDS:
            bucket[0] += 1
            bucket[1] += gap
        else:
            bucket[2] += 1
    return _write(conn, cfg.channel_id, "edit_speed", "channel_id, author_id, edits, total_seconds, capped_edits",
                  [(cfg.channel_id, author, e, s, capped)
                   for author, (e, s, capped) in per_author.items()])


def _parent_index(conn: sqlite3.Connection, cfg: Config) -> dict[str, tuple[str, str]]:
    """``message id -> (author_id, created_at)`` for every parent a reply needs.

    Every message is indexed, not only counted ones: a reply to a bot still has
    a latency, and an unfindable parent is a hole in the scrape rather than a
    statement about who replied.
    """
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute(
            "SELECT id, author_id, created_at FROM messages WHERE channel_id = ?",
            (cfg.channel_id,),
        )
    }


# Resolved through globals() at call time, so a caller (or a test) can replace
# one family's implementation and rebuild_all picks it up.
REBUILDER_NAMES: dict[str, str] = {family: f"rebuild_{family}" for family in FAMILIES}


def store_author_names(conn: sqlite3.Connection, cfg: Config, ids: Identities) -> None:
    """Remember each author's current display name.

    Refreshed on every rebuild, but never over a name an admin chose: ``renamed``
    is the difference between "Discord renamed them" and "we were told what to
    call them" (spec §5.5 keeps a rename cosmetic, so a rebuild must not undo it).
    """
    display = latest_display_names(conn)
    rows = []
    for accounts in ids.accounts:
        if not accounts:
            continue
        # Every account in a row maps to the same canonical, which is right even
        # when the canonical account itself never posted.
        canonical = ids.canonical[accounts[0]]
        rows.append((cfg.channel_id, canonical, display.get(canonical) or canonical))
    conn.execute("DELETE FROM author_names WHERE channel_id = ? AND renamed = 0",
                 (cfg.channel_id,))
    conn.executemany(
        """
        INSERT INTO author_names (channel_id, author_id, name, renamed)
        VALUES (?, ?, ?, 0)
        ON CONFLICT (channel_id, author_id) DO UPDATE SET name = excluded.name
         WHERE author_names.renamed = 0
        """,
        rows,
    )


def rebuild_all(conn: sqlite3.Connection, cfg: Config, *, now: str | None = None) -> dict[str, int]:
    """Rebuild every family for this channel inside one transaction.

    Returns row counts per family. A failure anywhere leaves the previous rows
    and the previous provenance exactly as they were.
    """
    create_rollup_tables(conn)
    create_control_tables(conn)   # author_names lives with the control plane
    ids = resolve_identities(conn, cfg)
    stamp = now or datetime.now().astimezone().isoformat(timespec="seconds")
    config_hash = config_digest(cfg)
    # The fingerprint is the whole channel, not the counted subset: provenance
    # answers "which state of messages produced these rows?", and staleness is a
    # comparison against the same thing. Fingerprinting the filtered subset would
    # let a bot's message leave the rollups reported current while the table they
    # were built from no longer exists. The *rules* (types, bots, merges) are
    # recorded separately, as config_hash.
    source_count, source_max = conn.execute(
        "SELECT COUNT(*), MAX(id) FROM messages WHERE channel_id = ?", (cfg.channel_id,)
    ).fetchone()

    counts: dict[str, int] = {}
    # A savepoint rather than BEGIN: the caller may already be inside a
    # transaction (a job runner, a test), where a nested BEGIN raises.
    conn.execute("SAVEPOINT rollup_rebuild")
    try:
        store_author_names(conn, cfg, ids)
        for family in FAMILIES:
            counts[family] = globals()[REBUILDER_NAMES[family]](conn, cfg, ids)
            set_provenance(
                conn, cfg.channel_id, family,
                computed_at=stamp,
                source_max_id=source_max or "",
                source_count=int(source_count or 0),
                timezone=cfg.timezone,
                config_hash=config_hash,
            )
    except BaseException:
        conn.execute("ROLLBACK TO rollup_rebuild")
        conn.execute("RELEASE rollup_rebuild")
        raise
    conn.execute("RELEASE rollup_rebuild")
    conn.commit()
    return counts


BUILDERS: dict[str, Callable[..., int]] = {
    family: globals()[name] for family, name in REBUILDER_NAMES.items()
}
