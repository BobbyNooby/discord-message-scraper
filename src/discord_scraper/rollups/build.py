"""Build every rollup from ``messages``.

Three rules shape this module:

* **Delete, then insert — never incremental.** A rebuild is the only way rows
  change, so a partially-updated rollup is impossible by construction.
* **One transaction for the whole rebuild.** Fourteen families and their
  provenance either all land or none do.
* **Read first, then write.** Every family is derived in a single pass over
  ``messages`` *before* the transaction opens. SQLite has one writer, so a
  multi-minute read inside a transaction would block every other writer in the
  process — session inserts, the admin board, the worker's heartbeat — until it
  timed out. The lock is now held for the inserts only.

The per-family functions stay independently callable, as the CLI's interface
requires; each derives what it needs when it is called on its own.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from discord_scraper.config import Config
from discord_scraper.rollups.identity import (
    Identities,
    latest_display_names,
    resolve_identities,
)
from discord_scraper.rollups.schema import (
    AUTHOR_FAMILIES,
    FAMILIES,
    create_control_tables,
    create_rollup_tables,
    set_provenance,
    stamp_schema_version,
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


def _parse_raw(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, AttributeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _array(parsed: dict[str, Any], key: str) -> list[Any]:
    value = parsed.get(key)
    return value if isinstance(value, list) else []


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


_MEDIA_SUFFIXES = (".mp4", ".webm", ".gif", ".gifv", ".png", ".jpg", ".jpeg", ".webp")


def _looks_like_media(url: str) -> bool:
    """A file we can actually show, not a page about one."""
    without_query = url.split("?", 1)[0].lower()
    return bool(url) and without_query.endswith(_MEDIA_SUFFIXES)


def _media_key(embed: dict[str, Any]) -> tuple[str, str, str, str, str] | None:
    """The identity of a posted embed: (url, kind, title, thumbnail, animated).

    Discord gives several shapes — a gifv is a video with an image poster, a
    YouTube link is a video with a thumbnail — so the URL falls back through
    video, image and thumbnail rather than assuming one field exists.
    """
    def _url(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("url") or value.get("proxy_url") or "")
        return str(value or "")

    url = str(embed.get("url") or "") or _url(embed.get("video")) or _url(embed.get("image"))
    thumbnail = _url(embed.get("thumbnail")) or _url(embed.get("image"))
    if not url and not thumbnail:
        return None
    kind = str(embed.get("type") or _UNKNOWN)
    title = str(embed.get("title") or "").strip()[:200]
    # `video` is the playable asset (an mp4 for a gifv); `image` is the animated
    # gif when there is no video. Either animates; the thumbnail often does not.
    #
    # But `video.url` is only sometimes a file: for YouTube it is the player page
    # (`www.youtube.com/embed/…`), which the thumbnail proxy rightly refuses. Keep
    # only a real asset.
    animated = _url(embed.get("video")) or _url(embed.get("image"))
    if not _looks_like_media(animated):
        animated = ""
    if not _looks_like_media(thumbnail):
        thumbnail = thumbnail if thumbnail else ""
    return (url or thumbnail, kind, title, thumbnail, animated)


def _name_tokens(conn: sqlite3.Connection, display: dict[str, str] | None = None) -> set[str]:
    """Tokens that are somebody's handle or display name (spec §6.2 always-on 3)."""
    names = set((display if display is not None else latest_display_names(conn)).values())
    names |= {row[0] for row in conn.execute(
        "SELECT DISTINCT author_username FROM messages WHERE author_username IS NOT NULL"
    )}
    found: set[str] = set()
    for name in names:
        found.update(tokens(str(name)))
    return found


def config_digest(cfg: Config, conn: sqlite3.Connection | None = None) -> str:
    """Fingerprint of every setting a rebuild reads.

    Public because `status` compares it: an edit to the stopword list, the
    timezone, the counted types, the bot rule or the identity map changes what
    the rollups *should* contain without adding a single source row, so the
    message fingerprint alone would report "current" while the tables are stale
    (§5.2 stores this column to make that comparison possible).
    """
    parts = [
        ",".join(sorted(cfg.stopword_packs)),
        ",".join(sorted(cfg.ignore_words)),
        ",".join(sorted(load_stopwords(cfg))),
        str(cfg.timezone),
        ",".join(str(t) for t in cfg.count_types),
        str(bool(cfg.exclude_bots)),
        ";".join(f"{k}:{','.join(sorted(v))}" for k, v in sorted((cfg.identities or {}).items())),
    ]
    if conn is not None:
        # Merges and "who shows" live in the database, not the config file, and
        # both change what every rollup should contain — so they belong in the
        # fingerprint too. Without this, hiding a person left `status` reporting
        # "current" over tables that were now wrong (§11.6's whole point).
        try:
            parts.append(";".join(
                f"{canonical}:{account}" for canonical, account in conn.execute(
                    "SELECT canonical_id, account_id FROM identity WHERE channel_id = ? "
                    "ORDER BY account_id",
                    (cfg.channel_id,),
                )
            ))
            parts.append(",".join(sorted(row[0] for row in conn.execute(
                "SELECT author_id FROM author_names WHERE channel_id = ? AND included = 0",
                (cfg.channel_id,),
            ))))
        except sqlite3.OperationalError:
            pass   # a fresh database has neither table yet
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


@dataclass
class Derived:
    """Everything one pass over ``messages`` can produce."""

    activity: Counter = field(default_factory=Counter)
    heat: Counter = field(default_factory=Counter)
    replies: Counter = field(default_factory=Counter)
    words: Counter = field(default_factory=Counter)
    links: Counter = field(default_factory=Counter)
    providers: Counter = field(default_factory=Counter)
    embed_types: Counter = field(default_factory=Counter)
    # url -> [uses, first_seen, last_seen, provider, kind, title, thumbnail].
    # Keyed on the URL alone: Discord reports the same GIF with a different
    # provider (or none) depending on the post, and "this GIF was posted 28
    # times" is a fact about the URL, not about the wrapper around it.
    # [uses, first, last, provider, kind, title, thumbnail, animated]
    media: dict[str, list[Any]] = field(default_factory=dict)
    # (word, author, month) -> uses: the timeline behind the all-time `words`.
    word_months: Counter = field(default_factory=Counter)
    # (url, author, month) -> [uses, first_seen, first_message_id]
    media_posts: dict[tuple[str, str, str], list[Any]] = field(default_factory=dict)
    emoji: Counter = field(default_factory=Counter)
    reactions: Counter = field(default_factory=Counter)
    attachments: Counter = field(default_factory=Counter)
    starters: Counter = field(default_factory=Counter)
    response: dict[str, list[int]] = field(default_factory=dict)
    edit_speed: dict[str, list[int]] = field(default_factory=dict)
    types: Counter = field(default_factory=Counter)
    link_msgs: int = 0
    link_both: int = 0
    hosts: set[str] = field(default_factory=set)
    source_count: int = 0
    source_max: str = ""
    # (child author, parent id, child created_at), resolved once the scan ends:
    # an unordered scan can meet a child before its parent.
    pending_replies: list[tuple[str, str, str]] = field(default_factory=list)
    parents: dict[str, tuple[str, str]] = field(default_factory=dict)
    # The earliest counted message of each local day, by (created_at, id).
    best_day: dict[str, tuple[str, str]] = field(default_factory=dict)


# A long derive used to be invisible: the run row said "started" and then the
# next thing written was the write phase, tens of seconds later. This is how
# often the scan says where it has got to, so the live feed shows progress
# rather than only elapsed time.
DERIVE_HEARTBEAT_SECONDS = 5.0
# Time is checked every this many rows rather than every row; the clock call is
# cheap but not free, and 256 rows is well under a millisecond of scanning.
DERIVE_HEARTBEAT_ROWS = 256


def derive(conn: sqlite3.Connection, cfg: Config, ids: Identities,
           on_progress: Callable[[int, float], None] | None = None) -> Derived:
    """One pass over the channel's messages, feeding all fourteen families.

    Streams rows rather than fetching them: ``raw`` is 483 MB on the reference
    dataset, and holding it in memory to save a scan would trade one problem for
    a worse one.

    ``on_progress(scanned, seconds)`` is called at most every
    ``DERIVE_HEARTBEAT_SECONDS`` while the scan runs, so a caller can write a
    heartbeat without the loop knowing what a heartbeat is.
    """
    d = Derived()
    display = latest_display_names(conn)
    stop = load_stopwords(cfg) | _name_tokens(conn, display)
    allowed = set(cfg.count_types) if cfg.count_types else None

    scan_started = time.monotonic()
    last_beat = scan_started
    checked = 0
    cursor = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM messages WHERE channel_id = ?", (cfg.channel_id,)
    )
    for row in cursor:
        checked += 1
        if on_progress is not None and checked % DERIVE_HEARTBEAT_ROWS == 0:
            now = time.monotonic()
            if now - last_beat >= DERIVE_HEARTBEAT_SECONDS:
                on_progress(d.source_count, now - scan_started)
                last_beat = now
        (mid, author_id, content, created, edited, reply_to, has_attachments, mtype,
         raw) = row
        d.source_count += 1
        if mid and str(mid) > d.source_max:
            d.source_max = str(mid)
        d.parents[str(mid)] = (author_id, created)

        parsed = _parse_raw(raw)
        author_obj = parsed.get("author")
        is_bot = isinstance(author_obj, dict) and (author_obj.get("bot") is True
                                                  or author_obj.get("bot") == 1)
        d.types[int(mtype if mtype is not None else -1)] += 1

        if allowed is not None and mtype not in allowed:
            continue
        if cfg.exclude_bots and is_bot:
            continue
        author = ids.canonical.get(author_id)
        if author is None:
            continue

        day, year, weekday, hour = local_parts(created, cfg.timezone)
        embeds = _array(parsed, "embeds")
        media = 1 if has_attachments else (1 if embeds else 0)
        body = content or ""

        cell = d.activity.setdefault((day, author), [0, 0, 0, 0, 0])
        cell[0] += 1
        cell[1] += len(body)
        cell[2] += media
        cell[3] += 1 if edited else 0
        cell[4] += 1 if mtype == 19 else 0

        d.heat[(year, (weekday + 1) % 7, hour, author)] += 1   # 0=Sun, like the spec

        previous = d.best_day.get(day)
        if previous is None or (created, str(mid)) < previous:
            d.best_day[day] = (created, str(mid))

        month = day[:7]
        for word in tokens(body):
            if word not in stop:
                d.words[(word, author)] += 1
                d.word_months[(word, author, month)] += 1

        found_urls = links(body)
        if found_urls:
            d.link_msgs += 1
            if embeds:
                d.link_both += 1
        for url in found_urls:
            parsed_link = normalise_link(url)
            if parsed_link is None:
                continue
            host, key = parsed_link
            d.hosts.add(host)
            d.links[(day, author, key)] += 1

        for symbol in emoji(body):
            d.emoji[(symbol, author)] += 1

        for embed in embeds:
            embed = embed if isinstance(embed, dict) else {}
            provider = (embed.get("provider") or {}).get("name") or _UNPROVIDENCED
            d.providers[(author, str(provider))] += 1
            d.embed_types[str(embed.get("type") or _UNKNOWN)] += 1

            # The media itself, so the same GIF posted 28 times is one row with a
            # count and a thumbnail rather than 28 anonymous embed events.
            media = _media_key(embed)
            if media is not None:
                url, kind, title, thumbnail, animated = media
                entry = d.media.setdefault(
                    url, [0, created, created, str(provider), kind, title, thumbnail, animated]
                )
                entry[0] += 1
                if created < entry[1]:
                    entry[1] = created
                if created > entry[2]:
                    entry[2] = created
                # Fill gaps rather than overwrite: the first sighting is not
                # always the one that carried a title or a thumbnail.
                if not entry[5] and title:
                    entry[5] = title
                if not entry[6] and thumbnail:
                    entry[6] = thumbnail
                if not entry[7] and animated:
                    entry[7] = animated
                if entry[3] == _UNPROVIDENCED and str(provider) != _UNPROVIDENCED:
                    entry[3] = str(provider)
                post = d.media_posts.setdefault((url, author, month), [0, created, str(mid)])
                post[0] += 1
                if (created, str(mid)) < (post[1], post[2]):
                    post[1], post[2] = created, str(mid)

        for reaction in _array(parsed, "reactions"):
            if not isinstance(reaction, dict):
                continue
            value = reaction.get("emoji")
            name = (str(value.get("name") or value.get("id") or _UNKNOWN)
                    if isinstance(value, dict) else str(value))
            d.reactions[(name, author)] += int(reaction.get("count") or 0)

        for attachment in _array(parsed, "attachments"):
            mime = str((attachment or {}).get("content_type") or _UNKNOWN)
            d.attachments[(mime, author)] += 1

        if reply_to:
            d.pending_replies.append((author, str(reply_to), created))

        if edited:
            gap = _seconds_between(created, edited)
            if gap is not None and gap >= 0:
                bucket = d.edit_speed.setdefault(author, [0, 0, 0])
                if gap <= LATENCY_CAP_SECONDS:
                    bucket[0] += 1
                    bucket[1] += gap
                else:
                    bucket[2] += 1

    for _day, (_created, mid) in d.best_day.items():
        parent_author = d.parents.get(mid, (None, None))[0]
        key = ids.canonical.get(parent_author) if parent_author else None
        if key is not None:
            d.starters[key] += 1

    for child, parent_id, child_created in d.pending_replies:
        parent = d.parents.get(parent_id)
        if parent is None:
            continue
        parent_author, parent_created = parent
        parent_key = ids.canonical.get(parent_author)
        if parent_key is not None:
            d.replies[(parent_key, child)] += 1
        gap = _seconds_between(parent_created, child_created)
        if gap is None or gap <= 0:
            continue
        bucket = d.response.setdefault(child, [0, 0, 0])
        if gap <= LATENCY_CAP_SECONDS:
            bucket[0] += 1
            bucket[1] += gap
        else:
            bucket[2] += 1

    return d


def store_author_names(
    conn: sqlite3.Connection,
    cfg: Config,
    ids: Identities,
    display: dict[str, str] | None = None,
) -> None:
    """Remember each author's current display name.

    Refreshed on every rebuild, but never over a name an admin chose: ``renamed``
    is the difference between "Discord renamed them" and "we were told what to
    call them" (spec §5.5 keeps a rename cosmetic, so a rebuild must not undo it).
    """
    display = display if display is not None else latest_display_names(conn)
    rows = []
    for accounts in ids.accounts:
        if not accounts:
            continue
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


def write_stats(conn: sqlite3.Connection, channel_id: str, derived: Derived) -> None:
    """Record the three link totals the capped grains cannot answer (§5.11)."""
    conn.execute("DELETE FROM rollup_stats WHERE channel_id = ?", (channel_id,))
    conn.executemany(
        "INSERT INTO rollup_stats (channel_id, key, value) VALUES (?, ?, ?)",
        [(channel_id, "link_msgs", derived.link_msgs),
         (channel_id, "link_hosts", len(derived.hosts)),
         (channel_id, "link_both", derived.link_both)],
    )


# --- families ----------------------------------------------------------------
# Each takes an optional pre-derived bundle: `rebuild_all` passes one so the
# channel is scanned once, and a caller that wants a single family gets a scan
# of its own without a second code path.

def rebuild_type(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "type", "channel_id, type, msgs",
                  [(cfg.channel_id, int(k), n) for k, n in d.types.items()])


def rebuild_activity(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "activity",
                  "channel_id, day, author_id, msgs, chars, media, edits, replies",
                  [(cfg.channel_id, day, author, *counts)
                   for (day, author), counts in d.activity.items()])


def rebuild_heat(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "heat", "channel_id, year, dow, hour, author_id, msgs",
                  [(cfg.channel_id, year, dow, hour, author, n)
                   for (year, dow, hour, author), n in d.heat.items()])


def rebuild_reply(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "reply",
                  "channel_id, parent_author_id, child_author_id, msgs",
                  [(cfg.channel_id, parent, child, n) for (parent, child), n in d.replies.items()])


def _replace_rows(conn: sqlite3.Connection, table: str, channel_id: str, columns: str,
                  rows: Iterable[tuple]) -> None:
    """``_write`` for a table that rides along with a family rather than being one."""
    conn.execute(f"DELETE FROM {table} WHERE channel_id = ?", (channel_id,))
    placeholders = ", ".join("?" for _ in range(len(columns.split(","))))
    conn.executemany(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", rows)


def rebuild_word(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    _replace_rows(conn, "rollup_word_month", cfg.channel_id,
                  "channel_id, word, author_id, month, uses",
                  ((cfg.channel_id, word, author, month, n)
                   for (word, author, month), n in d.word_months.items()))
    return _write(conn, cfg.channel_id, "word", "channel_id, word, author_id, uses",
                  [(cfg.channel_id, word, author, n) for (word, author), n in d.words.items()])


def rebuild_link(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    per_host: Counter[str] = Counter()
    for (_day, _author, key), uses in d.links.items():
        per_host[key.split("/", 1)[0]] += uses
    keep = {host for host, _ in per_host.most_common(TOP_DOMAINS)}
    return _write(conn, cfg.channel_id, "link", "channel_id, day, author_id, domain, uses",
                  [(cfg.channel_id, day, author, key, n)
                   for (day, author, key), n in d.links.items()
                   if key.split("/", 1)[0] in keep])


def rebuild_provider(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "provider", "channel_id, author_id, provider, embeds",
                  [(cfg.channel_id, author, provider, n)
                   for (author, provider), n in d.providers.items()])


def rebuild_embed_type(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "embed_type", "channel_id, embed_type, embeds",
                  [(cfg.channel_id, kind, n) for kind, n in d.embed_types.items()])


def rebuild_media(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    _replace_rows(conn, "rollup_media_post", cfg.channel_id,
                  "channel_id, url, author_id, month, uses, first_seen, first_message_id",
                  ((cfg.channel_id, url, author, month, uses, first, mid)
                   for (url, author, month), (uses, first, mid) in d.media_posts.items()))
    return _write(conn, cfg.channel_id,
                  "media",
                  "channel_id, url, provider, kind, title, thumbnail, animated, uses, first_seen, last_seen",
                  [(cfg.channel_id, url, entry[3], entry[4], entry[5], entry[6], entry[7],
                    entry[0], entry[1], entry[2])
                   for url, entry in d.media.items()])


def rebuild_emoji(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "emoji", "channel_id, emoji, author_id, uses",
                  [(cfg.channel_id, symbol, author, n)
                   for (symbol, author), n in d.emoji.items()])


def rebuild_reaction(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "reaction", "channel_id, emoji, author_id, count",
                  [(cfg.channel_id, name, author, n)
                   for (name, author), n in d.reactions.items()])


def rebuild_attachment(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "attachment", "channel_id, mime, author_id, count",
                  [(cfg.channel_id, mime, author, n)
                   for (mime, author), n in d.attachments.items()])


def rebuild_response(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "response",
                  "channel_id, author_id, replies, total_seconds, capped_replies",
                  [(cfg.channel_id, author, r, s, capped)
                   for author, (r, s, capped) in d.response.items()])


def rebuild_starter(conn: sqlite3.Connection, cfg: Config, ids: Identities, derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "starter", "channel_id, author_id, starters",
                  [(cfg.channel_id, author, n) for author, n in d.starters.items()])


def rebuild_edit_speed(conn: sqlite3.Connection, cfg: Config, ids: Identities,
                       derived=None) -> int:
    d = derived or derive(conn, cfg, ids)
    return _write(conn, cfg.channel_id, "edit_speed",
                  "channel_id, author_id, edits, total_seconds, capped_edits",
                  [(cfg.channel_id, author, e, s, capped)
                   for author, (e, s, capped) in d.edit_speed.items()])


# Resolved through globals() at call time, so a caller (or a test) can replace
# one family's implementation and rebuild_all picks it up.
REBUILDER_NAMES: dict[str, str] = {family: f"rebuild_{family}" for family in FAMILIES}


def empty_author_rollups(counts: dict[str, int]) -> bool:
    """True when no family keyed on a person got a row.

    Writing empty rollups is legitimate — the operator may really have excluded
    everybody — so this is a signal for the caller to explain, not a failure.
    """
    return all(counts.get(family, 0) == 0 for family in AUTHOR_FAMILIES)


# How many progress rows survive. A rebuild writes ~32 (a run pair plus one pair
# per family), so this is roughly the last fifteen rebuilds — plenty for "what
# just happened", and a hard ceiling on a table the worker writes forever.
EVENTS_KEEP = 500


def prune_rebuild_events(conn: sqlite3.Connection, keep: int = EVENTS_KEEP) -> int:
    """Drop all but the newest ``keep`` progress rows. Returns how many went."""
    cursor = conn.execute(
        "DELETE FROM rebuild_events WHERE id NOT IN "
        "(SELECT id FROM rebuild_events ORDER BY id DESC LIMIT ?)",
        (int(keep),),
    )
    return int(cursor.rowcount or 0)


def _record_events(conn: sqlite3.Connection, events: Iterable[tuple]) -> None:
    """Append progress rows and prune, in one transaction.

    Deliberately a plain function on the rebuild's own connection: the events
    are evidence about a rebuild, not part of its atomic rollup write, so a
    failed write must still leave the log behind.
    """
    conn.executemany(
        "INSERT INTO rebuild_events "
        "(started_at, finished_at, kind, family, phase, message, rows, seconds, "
        " level, source, scope) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        list(events),
    )
    prune_rebuild_events(conn)
    conn.commit()


def record_event(
    conn: sqlite3.Connection,
    *,
    message: str,
    source: str,
    scope: str,
    level: str = "info",
    kind: str = "op",
    family: str | None = None,
    phase: str | None = None,
    rows: int | None = None,
    seconds: float | None = None,
    started_at: str | None = None,
    finished_at: str | None = None,
) -> None:
    """Write one control-plane log line.

    The one writer for everything that is not a rebuild's own progress, so the
    retention policy stays in one place: every write prunes, exactly like a
    rebuild's, and a long-lived worker cannot grow the table past ``EVENTS_KEEP``.
    Callers pass counts, ids, names and timings only — never message text or a
    media URL.
    """
    _record_events(conn, [(
        started_at or _iso_now(), finished_at, kind, family, phase, message,
        rows, seconds, level, source, scope,
    )])


def _iso_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def rebuild_all(
    conn: sqlite3.Connection,
    cfg: Config,
    *,
    now: str | None = None,
    on_phase: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Rebuild every family for this channel.

    Derivation happens outside the transaction and the writes inside it: the
    rollups either all land or none do, and the write lock is held for the
    inserts rather than for the reading.

    ``on_phase`` is called as the rebuild moves between phases (``deriving``,
    ``writing``, ``finished``, ``failed``) so a caller with a job row can say
    what an in-flight rebuild is doing. The CLI passes nothing and its output is
    unchanged.
    """
    # The control plane owns `rebuild_events`, so it has to exist before the run
    # can be logged. Everything else in the prelude — the rollup tables, the
    # identity resolution, the config hash — takes tens of seconds on a real
    # database, and the run must be visible for all of it rather than only once
    # the derive begins.
    create_control_tables(conn)   # author_names lives with the control plane
    # Microseconds, not seconds: the API caches its payload keyed on provenance,
    # and two rebuilds inside the same second would otherwise be indistinguishable
    # — which is exactly how a stale payload gets served after a rebuild.
    stamp = now or datetime.now().astimezone().isoformat(timespec="microseconds")

    def phase(name: str) -> None:
        if on_phase is not None:
            on_phase(name)

    # Committed on its own, before the prelude and the long derive: while a
    # rebuild is running this row is the only thing the board has to read, so it
    # cannot wait for the rollup transaction to end.
    run_clock = time.monotonic()
    _record_events(conn, [(stamp, None, "run", None, "started", "rebuild started", None, None,
                           "info", "rebuild", "rollup")])

    def heartbeat(scanned: int, seconds: float) -> None:
        """Say the derive is still moving, so a 20–40 s scan is visible by itself.

        A plain ``op`` row, not a ``run`` row: the run row's phase is what the
        board reads to decide "running", and a heartbeat must not be mistaken for
        a second run starting.
        """
        record_event(conn, kind="op", phase="deriving", source="rebuild", scope="rollup",
                     message=f"deriving: {scanned} messages scanned in {seconds:.1f}s",
                     rows=scanned, seconds=round(seconds, 3))

    try:
        phase("deriving")
        create_rollup_tables(conn)
        ids = resolve_identities(conn, cfg)
        config_hash = config_digest(cfg, conn)
        derived = derive(conn, cfg, ids, on_progress=heartbeat)

        # Mark every family in flight before the write transaction opens. It is
        # committed a moment early, but it is what makes a poll during the write
        # phase see work rather than a blank list. The finished rows below carry
        # the real row count and the seconds each family took.
        phase("writing")
        _record_events(conn, [
            (_iso_now(), None, "family", family, "started", f"{family} started", None, None,
             "info", "rebuild", "rollup")
            for family in FAMILIES
        ])

        counts: dict[str, int] = {}
        family_seconds: dict[str, tuple[str, float]] = {}
        # A savepoint rather than BEGIN: the caller may already be inside a
        # transaction (a job runner, a test), where a nested BEGIN raises.
        conn.execute("SAVEPOINT rollup_rebuild")
        try:
            store_author_names(conn, cfg, ids)
            write_stats(conn, cfg.channel_id, derived)
            for family in FAMILIES:
                family_start = time.monotonic()
                family_at = _iso_now()
                counts[family] = globals()[REBUILDER_NAMES[family]](conn, cfg, ids, derived)
                family_seconds[family] = (family_at, time.monotonic() - family_start)
                set_provenance(
                    conn, cfg.channel_id, family,
                    computed_at=stamp,
                    source_max_id=derived.source_max,
                    source_count=derived.source_count,
                    timezone=cfg.timezone,
                    config_hash=config_hash,
                )
        except BaseException:
            conn.execute("ROLLBACK TO rollup_rebuild")
            conn.execute("RELEASE rollup_rebuild")
            raise
        conn.execute("RELEASE rollup_rebuild")
        conn.commit()
        # The rebuild is the thing that decides what the numbers mean, so the
        # stamp belongs with it: a successful build is what makes the database
        # "this revision" rather than whatever it was before.
        stamp_schema_version(conn)
    except BaseException as exc:
        # A failed rebuild that left no trace is the silent failure this log
        # exists to prevent, so record it and then re-raise unchanged.
        _record_failure(conn, stamp, run_clock, exc)
        try:
            phase("failed")
        except Exception:
            pass
        raise

    finished_at = _iso_now()
    total_rows = sum(counts.values())
    elapsed = time.monotonic() - run_clock
    _record_events(conn, [
        (family_seconds[family][0], finished_at, "family", family, "finished",
         f"{family} {counts[family]} rows rebuilt", counts[family],
         round(family_seconds[family][1], 3), "info", "rebuild", "rollup")
        for family in FAMILIES
    ] + [
        (stamp, finished_at, "run", None, "finished",
         f"{total_rows} rows rebuilt in {elapsed:.1f}s", total_rows, round(elapsed, 3),
         "info", "rebuild", "rollup"),
    ])
    phase("finished")
    return counts


def _record_failure(conn: sqlite3.Connection, stamp: str, run_clock: float,
                    exc: BaseException) -> None:
    """Log a failed run. Never masks the original exception."""
    try:
        _record_events(conn, [(
            stamp, _iso_now(), "run", None, "failed",
            f"rebuild failed: {type(exc).__name__}: {exc}", None,
            round(time.monotonic() - run_clock, 3), "error", "rebuild", "rollup",
        )])
    except sqlite3.Error:
        pass


BUILDERS: dict[str, Callable[..., int]] = {
    family: globals()[name] for family, name in REBUILDER_NAMES.items()
}
