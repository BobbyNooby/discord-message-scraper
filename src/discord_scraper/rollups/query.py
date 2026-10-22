"""Read helpers over the rollup tables.

Every function here reads only ``rollup_*``. That is the whole point of the
design (spec §5.3): the API assembles its payload from precomputed grains, so a
request never scans ``messages`` — and the 740 MB of raw JSON is never in the
read path.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from discord_scraper.config import Config
from discord_scraper.rollups import identity

FAMILIES_ORDERED: tuple[str, ...] = (
    "activity", "heat", "reply", "word", "link", "provider", "embed_type",
    "emoji", "reaction", "attachment", "response", "starter", "edit_speed", "type",
)

# Discord's message types, for the type doughnut's labels. Unknown ids fall back
# to "type N" rather than being dropped: a distribution that hides a bucket is
# worse than one with an ugly label.
TYPE_LABELS: dict[int, str] = {
    0: "default",
    1: "recipient add",
    2: "recipient remove",
    3: "call",
    4: "channel name",
    5: "channel icon",
    6: "pinned",
    7: "join",
    8: "boost",
    9: "boost tier 1",
    10: "boost tier 2",
    11: "boost tier 3",
    12: "follow add",
    14: "discovery",
    18: "thread created",
    19: "reply",
    20: "command",
    21: "thread starter",
    22: "invite reminder",
}


def type_label(type_id: int) -> str:
    return TYPE_LABELS.get(type_id, f"type {type_id}")


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[tuple]:
    return [tuple(row) for row in conn.execute(sql, params).fetchall()]


def days(conn: sqlite3.Connection, channel_id: str) -> list[str]:
    return [row[0] for row in rows(
        conn, "SELECT DISTINCT day FROM rollup_activity WHERE channel_id = ? ORDER BY day",
        (channel_id,),
    )]


def authors(conn: sqlite3.Connection, channel_id: str,
            cfg: Config | None = None) -> list[dict[str, Any]]:
    """Canonical authors in the same order as every ``author_id``-keyed rollup.

    A rollup row is keyed by whichever account sent the message, so an
    un-rebuilt table can still carry a merged-away alt and present one person
    twice. Here each account is folded into its canonical id and the volumes are
    summed, so the list is canonical-only no matter what state the tables are in
    (spec §5.5). ``accounts`` records every raw id folded into the row, and
    callers build their author index from it so an alt's cells still land on the
    canonical's index.

    Ordered by message volume, then id, because the frontend colours series by
    position and a rebuild must not reshuffle the palette. Names come from
    ``author_names`` — never from ``messages``, which a request must not scan.
    """
    merge = identity.canonical_map(conn, channel_id, cfg)
    totals: dict[str, int] = {}
    account_msgs: dict[str, int] = {}
    for author_id, msgs in rows(
        conn,
        """
        SELECT author_id, SUM(msgs) AS msgs FROM rollup_activity
         WHERE channel_id = ? GROUP BY author_id
        """,
        (channel_id,),
    ):
        account_msgs[author_id] = int(msgs)
        canonical = merge.get(author_id, author_id)
        totals[canonical] = totals.get(canonical, 0) + int(msgs)

    stored_names = dict(rows(
        conn, "SELECT author_id, name FROM author_names WHERE channel_id = ?", (channel_id,)
    ))
    members: dict[str, set[str]] = {canonical: {canonical} for canonical in totals}
    for account, canonical in merge.items():
        if canonical in members:
            members[canonical].add(account)

    def display_name(canonical: str) -> str:
        if canonical in stored_names:
            return stored_names[canonical]
        # No row for the canonical itself: borrow the busiest alias's name
        # rather than showing a snowflake.
        aliases = sorted((a for a in members[canonical] if a in stored_names),
                         key=lambda a: (-account_msgs.get(a, 0), a))
        return stored_names[aliases[0]] if aliases else canonical

    ordered = sorted(totals, key=lambda who: (-totals[who], who))
    return [
        {
            "id": who,
            "name": display_name(who),
            "msgs": totals[who],
            "accounts": [who, *sorted(a for a in members[who] if a != who)],
        }
        for who in ordered
    ]


def author_index(author_rows: list[dict[str, Any]]) -> dict[str, int]:
    """``account_id -> row`` for every account folded into each canonical row.

    Built from ``authors``' ``accounts`` rather than from the identity table
    again, so the index is exactly as wide as the list the caller will render.
    """
    return {
        account: index
        for index, row in enumerate(author_rows)
        for account in row["accounts"]
    }


def activity_cells(conn, channel_id: str, day_index: dict[str, int], author_index: dict[str, int]):
    # Author rows can share a canonical index when the tables predate a merge,
    # so cells are summed per (day, author) instead of shipped twice.
    combined: dict[tuple[int, int], list[int]] = {}
    for day, author_id, msgs, chars, media, edits, replies in rows(
        conn,
        """
        SELECT day, author_id, msgs, chars, media, edits, replies
          FROM rollup_activity WHERE channel_id = ? ORDER BY day, author_id
        """,
        (channel_id,),
    ):
        if day not in day_index or author_id not in author_index:
            continue
        key = (day_index[day], author_index[author_id])
        cell = combined.setdefault(key, [0, 0, 0, 0, 0])
        cell[0] += msgs        # msgs
        cell[1] += chars       # chars
        cell[2] += replies     # replies
        cell[3] += media       # media
        cell[4] += edits       # edits
    return [[day, author, *counts] for (day, author), counts in combined.items()]


def heat_cells(conn, channel_id: str, author_index: dict[str, int]):
    combined: dict[tuple[int, int, int, int], int] = {}
    for year, dow, hour, author_id, msgs in rows(
        conn,
        """
        SELECT year, dow, hour, author_id, msgs FROM rollup_heat
         WHERE channel_id = ? ORDER BY year, dow, hour, author_id
        """,
        (channel_id,),
    ):
        if author_id not in author_index:
            continue
        key = (dow, hour, author_index[author_id], year)
        combined[key] = combined.get(key, 0) + msgs
    return [[dow, hour, author, year, msgs]
            for (dow, hour, author, year), msgs in combined.items()]


def reply_edges(conn, channel_id: str, author_index: dict[str, int]):
    combined: dict[tuple[int, int], int] = {}
    for parent, child, msgs in rows(
        conn,
        """
        SELECT parent_author_id, child_author_id, msgs FROM rollup_reply
         WHERE channel_id = ? ORDER BY parent_author_id, child_author_id
        """,
        (channel_id,),
    ):
        if parent not in author_index or child not in author_index:
            continue
        key = (author_index[parent], author_index[child])
        combined[key] = combined.get(key, 0) + msgs
    return [[parent, child, msgs] for (parent, child), msgs in combined.items()]


def response_times(conn, channel_id: str, author_index: dict[str, int]):
    # Seconds and replies are summed before the average is taken: averaging two
    # already-rounded averages would be wrong the moment a merge splits a
    # person across accounts.
    combined: dict[int, list[int]] = {}
    for author_id, replies, seconds, capped in rows(
        conn,
        """
        SELECT author_id, replies, total_seconds, capped_replies FROM rollup_response
         WHERE channel_id = ? ORDER BY author_id
        """,
        (channel_id,),
    ):
        if author_id not in author_index or not replies:
            continue
        cell = combined.setdefault(author_index[author_id], [0, 0])
        cell[0] += seconds
        cell[1] += replies
    return [[author, round(seconds / replies, 1), replies]
            for author, (seconds, replies) in combined.items()]


def edit_speeds(conn, channel_id: str, author_index: dict[str, int]):
    combined: dict[int, list[int]] = {}
    for author_id, edits, seconds, capped in rows(
        conn,
        """
        SELECT author_id, edits, total_seconds, capped_edits FROM rollup_edit_speed
         WHERE channel_id = ? ORDER BY author_id
        """,
        (channel_id,),
    ):
        if author_id not in author_index or not edits:
            continue
        cell = combined.setdefault(author_index[author_id], [0, 0])
        cell[0] += seconds
        cell[1] += edits
    return [[author, round(seconds / edits, 1), edits]
            for author, (seconds, edits) in combined.items()]


def starters(conn, channel_id: str, author_index: dict[str, int]):
    combined: dict[int, int] = {}
    for author_id, count in rows(
        conn,
        "SELECT author_id, starters FROM rollup_starter WHERE channel_id = ? ORDER BY author_id",
        (channel_id,),
    ):
        if author_id not in author_index:
            continue
        index = author_index[author_id]
        combined[index] = combined.get(index, 0) + count
    return [[author, count] for author, count in combined.items()]


def word_author_uses(conn, channel_id: str,
                     author_index: dict[str, int]) -> dict[tuple[str, int], int]:
    """``(word, author) -> uses`` with each author folded to its canonical index.

    Two accounts of one person can each have a row for the same word; those
    combine, so the per-word breakdown cannot count the person twice.
    """
    combined: dict[tuple[str, int], int] = {}
    for word, author_id, uses in rows(
        conn,
        """
        SELECT word, author_id, SUM(uses) FROM rollup_word
         WHERE channel_id = ? GROUP BY word, author_id
        """,
        (channel_id,),
    ):
        index = author_index.get(author_id)
        if index is None:
            continue
        key = (word, index)
        combined[key] = combined.get(key, 0) + int(uses)
    return combined


def top_words(conn, channel_id: str, limit: int = 500) -> list[tuple[str, int]]:
    return rows(
        conn,
        """
        SELECT word, SUM(uses) AS uses FROM rollup_word
         WHERE channel_id = ? GROUP BY word ORDER BY uses DESC, word LIMIT ?
        """,
        (channel_id, limit),
    )


def words_for(conn, channel_id: str, word: str) -> list[tuple[str, int]]:
    """``[(canonical_author, uses), ...]``, biggest first.

    Canonical for the same reason as ``authors``: ``/api/words`` names its
    results through ``authors``, so an alt id left in this list would fall back
    to a raw snowflake instead of the person it was merged into.
    """
    merge = identity.canonical_map(conn, channel_id)
    combined: dict[str, int] = {}
    for author_id, uses in rows(
        conn,
        """
        SELECT author_id, SUM(uses) FROM rollup_word
         WHERE channel_id = ? AND word = ? GROUP BY author_id
        """,
        (channel_id, word),
    ):
        canonical = merge.get(author_id, author_id)
        combined[canonical] = combined.get(canonical, 0) + int(uses)
    return sorted(combined.items(), key=lambda row: (-row[1], row[0]))


def _built(conn, table: str, channel_id: str) -> bool:
    """True once a rebuild has written ``table`` for this channel.

    The month tables arrived after the rest, so a database rebuilt before them
    has none (or an empty one); the caller says "recalculate" instead of
    showing an empty chart as if nobody ever said the word.
    """
    try:
        return bool(rows(conn, f"SELECT 1 FROM {table} WHERE channel_id = ? LIMIT 1",
                         (channel_id,)))
    except sqlite3.OperationalError:
        return False


def word_timeline(conn, channel_id: str, word: str) -> list[tuple[str, str, int]] | None:
    """``[(month, canonical_author, uses), ...]`` oldest first, or ``None`` if not built."""
    if not _built(conn, "rollup_word_month", channel_id):
        return None
    merge = identity.canonical_map(conn, channel_id)
    combined: dict[tuple[str, str], int] = {}
    for month, author_id, uses in rows(
        conn,
        """
        SELECT month, author_id, SUM(uses) FROM rollup_word_month
         WHERE channel_id = ? AND word = ? GROUP BY month, author_id
        """,
        (channel_id, word),
    ):
        key = (str(month), merge.get(author_id, author_id))
        combined[key] = combined.get(key, 0) + int(uses)
    return sorted(((month, author, uses) for (month, author), uses in combined.items()),
                  key=lambda row: (row[0], -row[2], row[1]))


def word_author_uses_between(
    conn,
    channel_id: str,
    author_index: dict[str, int],
    first_month: str | None = None,
    last_month: str | None = None,
    years: tuple[int, ...] = (),
) -> dict[tuple[str, int], int] | None:
    """``word_author_uses`` for a stretch of local months, ``None`` if not built.

    ``first_month``/``last_month`` are YYYY-MM bounds (inclusive, either may be
    open) and ``years`` keeps only those calendar years — the same two rules the
    dashboard's time filter applies to days, at month grain.
    """
    if not _built(conn, "rollup_word_month", channel_id):
        return None
    if years:
        # Years alone gave the planner no range on `month`, so it walked the
        # whole table by channel (8 s cold on the reference database). Bounding
        # by the first and last year keeps it on the month index; the IN below
        # still drops the years in between that were not picked.
        low, high = f"{min(years):04d}-01", f"{max(years):04d}-12"
        first_month = max(first_month, low) if first_month else low
        last_month = min(last_month, high) if last_month else high
    where = ["channel_id = ?"]
    params: list[Any] = [channel_id]
    if first_month:
        where.append("month >= ?")
        params.append(first_month)
    if last_month:
        where.append("month <= ?")
        params.append(last_month)
    if years:
        where.append(f"substr(month, 1, 4) IN ({', '.join('?' for _ in years)})")
        params.extend(f"{int(year):04d}" for year in years)
    combined: dict[tuple[str, int], int] = {}
    for word, author_id, uses in rows(
        conn,
        f"""
        SELECT word, author_id, SUM(uses) FROM rollup_word_month
         WHERE {' AND '.join(where)} GROUP BY word, author_id
        """,
        tuple(params),
    ):
        index = author_index.get(author_id)
        if index is None:
            continue
        key = (word, index)
        combined[key] = combined.get(key, 0) + int(uses)
    return combined


def word_suggestions(conn, channel_id: str, prefix: str, limit: int = 8) -> list[tuple[str, int]]:
    """The most-used indexed words starting with ``prefix``.

    A range on the word index rather than ``LIKE``, which SQLite cannot serve
    from an index while it is case-insensitive. Every word is stored lowercase.
    """
    prefix = prefix.strip().lower()
    if not prefix:
        return []
    return [(str(word), int(uses)) for word, uses in rows(
        conn,
        """
        SELECT word, SUM(uses) AS total FROM rollup_word
         WHERE channel_id = ? AND word >= ? AND word < ?
         GROUP BY word ORDER BY total DESC, word LIMIT ?
        """,
        (channel_id, prefix, prefix + "￿", limit),
    )]


def media_posts(conn, channel_id: str, url: str) -> list[tuple[str, str, int, str, str]] | None:
    """``[(month, canonical_author, uses, first_seen, first_message_id), ...]`` for one URL.

    Oldest first; ``None`` when the per-person table has not been built yet.
    """
    if not _built(conn, "rollup_media_post", channel_id):
        return None
    merge = identity.canonical_map(conn, channel_id)
    combined: dict[tuple[str, str], list[Any]] = {}
    for month, author_id, uses, first_seen, message_id in rows(
        conn,
        """
        SELECT month, author_id, uses, first_seen, first_message_id FROM rollup_media_post
         WHERE channel_id = ? AND url = ?
        """,
        (channel_id, url),
    ):
        key = (str(month), merge.get(author_id, author_id))
        entry = combined.setdefault(key, [0, str(first_seen), str(message_id)])
        entry[0] += int(uses)
        if str(first_seen) < entry[1]:
            entry[1], entry[2] = str(first_seen), str(message_id)
    return sorted(((month, author, uses, first, mid)
                   for (month, author), (uses, first, mid) in combined.items()),
                  key=lambda row: (row[0], row[3]))


def media_item(conn, channel_id: str, url: str) -> list | None:
    """One ``rollup_media`` row in ``top_media``'s shape, plus first and last seen."""
    found = rows(
        conn,
        """
        SELECT url, provider, kind, title, thumbnail, animated, uses, first_seen, last_seen
          FROM rollup_media WHERE channel_id = ? AND url = ?
        """,
        (channel_id, url),
    )
    if not found:
        return None
    url_, provider, kind, title, thumbnail, animated, uses, first_seen, last_seen = found[0]
    return [str(url_), str(provider), str(kind), str(title or ""), str(thumbnail or ""),
            str(animated or ""), int(uses or 0), first_seen, last_seen]


def distinct_word_count(conn, channel_id: str) -> int:
    found = rows(conn, "SELECT COUNT(DISTINCT word) FROM rollup_word WHERE channel_id = ?",
                 (channel_id,))
    return int(found[0][0]) if found else 0


def link_tables(conn, channel_id: str):
    """``(domains, cells, top_links)`` the three pieces the link panels need."""
    host_keys = rows(
        conn,
        """
        SELECT domain, SUM(uses) FROM rollup_link WHERE channel_id = ?
         GROUP BY domain ORDER BY SUM(uses) DESC, domain
        """,
        (channel_id,),
    )
    hosts: list[str] = []
    index: dict[str, int] = {}
    for key, _uses in host_keys:
        host = key.split("/", 1)[0]
        if host not in index:
            index[host] = len(hosts)
            hosts.append(host)
    return hosts, index, host_keys


def link_cells(conn, channel_id: str, day_index, author_index, host_index):
    combined: dict[tuple[int, int, int], int] = {}
    for day, author_id, key, uses in rows(
        conn,
        """
        SELECT day, author_id, domain, uses FROM rollup_link
         WHERE channel_id = ? ORDER BY day, author_id, domain
        """,
        (channel_id,),
    ):
        host = key.split("/", 1)[0]
        if day not in day_index or author_id not in author_index or host not in host_index:
            continue
        cell = (day_index[day], author_index[author_id], host_index[host])
        combined[cell] = combined.get(cell, 0) + uses
    return [[day, author, host, uses] for (day, author, host), uses in combined.items()]


def link_stats(conn, channel_id: str) -> dict[str, int]:
    """The three numbers under "Where the chat lives" (spec §5.11).

    Read from ``rollup_stats``, not counted here: all three are measured over
    *all* links, while ``rollup_link`` ships only the top-60 hosts and
    ``rollup_provider`` counts embed objects rather than messages. Deriving them
    from the capped grains gave 9,267 "domains" against a reference of 539.
    """
    stored = dict(rows(
        conn, "SELECT key, value FROM rollup_stats WHERE channel_id = ?", (channel_id,)
    ))
    return {
        "msgs": int(stored.get("link_msgs", 0)),
        "domains": int(stored.get("link_hosts", 0)),
        "both": int(stored.get("link_both", 0)),
    }


def counted_total(conn, channel_id: str) -> int:
    """The headline: messages the configured rules count (spec §18.1).

    Deliberately *not* the size of the ``messages`` table — that figure includes
    bots, joins and calls, and would disagree with `discord-scraper analyze
    stats` about the one number a reader takes away.
    """
    found = rows(conn, "SELECT SUM(msgs) FROM rollup_activity WHERE channel_id = ?", (channel_id,))
    return int(found[0][0] or 0) if found else 0


def top_media(conn, channel_id: str, limit: int = 24,
              provider: str | None = None, kind: str | None = None) -> list[list]:
    """``[[url, provider, kind, title, thumbnail, animated, uses], ...]``.

    Ranking everything together means GIFs win every slot, because a GIF gets
    reposted far more than a video link — so the caller can ask for one provider
    and see YouTube's or Twitter's own top instead. ``kind`` narrows further,
    and it is a WHERE clause rather than a filter over the page: a client-side
    filter would only ever sort the fifty rows the limit happened to return.
    """
    where = "channel_id = ?"
    params: list[Any] = [channel_id]
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    params.append(limit)
    return [
        [str(url), str(provider_), str(kind_), str(title or ""), str(thumbnail or ""),
         str(animated or ""), int(uses or 0)]
        for url, provider_, kind_, title, thumbnail, animated, uses in rows(
            conn,
            f"""
            SELECT url, provider, kind, title, thumbnail, animated, uses FROM rollup_media
             WHERE {where} ORDER BY uses DESC, url LIMIT ?
            """,
            tuple(params),
        )
    ]


def media_kinds(conn, channel_id: str, provider: str | None = None) -> list[str]:
    """Every kind present for this channel (or one provider), biggest first.

    The chips come from here, not from the page of rows the client already has:
    a kind that exists only below the limit is still a filter worth offering.
    """
    where = "channel_id = ?"
    params: list[Any] = [channel_id]
    if provider:
        where += " AND provider = ?"
        params.append(provider)
    return [
        str(kind)
        for (kind,) in rows(
            conn,
            f"""
            SELECT kind FROM rollup_media
             WHERE {where} AND kind <> ''
             GROUP BY kind ORDER BY SUM(uses) DESC, kind
            """,
            tuple(params),
        )
    ]


def pairs(conn, channel_id: str, table: str, label: str, value: str, limit: int = 30):
    """``[[label, value], ...]`` for the simple doughnut and bar panels."""
    return [
        [str(a), int(b or 0)]
        for a, b in rows(
            conn,
            f"""
            SELECT {label}, SUM({value}) AS total FROM {table}
             WHERE channel_id = ? GROUP BY {label} ORDER BY total DESC, {label} LIMIT ?
            """,
            (channel_id, limit),
        )
    ]


def type_rows(conn, channel_id: str) -> list[list]:
    return [
        [int(type_id), type_label(int(type_id)), int(msgs)]
        for type_id, msgs in rows(
            conn,
            "SELECT type, msgs FROM rollup_type WHERE channel_id = ? ORDER BY msgs DESC, type",
            (channel_id,),
        )
    ]


def identity_groups(conn, channel_id: str) -> list[dict[str, Any]]:
    """Merged people, as the admin board's "who is who" list wants them."""
    grouped: dict[str, list[str]] = {}
    for canonical, account in rows(
        conn,
        "SELECT canonical_id, account_id FROM identity WHERE channel_id = ? ORDER BY canonical_id, account_id",
        (channel_id,),
    ):
        grouped.setdefault(canonical, []).append(account)
    return [{"canonical": canonical, "members": members}
            for canonical, members in sorted(grouped.items())]


def provenance_row(conn, channel_id: str, family: str) -> dict[str, Any] | None:
    found = rows(
        conn,
        """
        SELECT computed_at, source_count, source_max_message_id, timezone, config_hash
          FROM rollup_meta WHERE channel_id = ? AND family = ?
        """,
        (channel_id, family),
    )
    if not found:
        return None
    computed_at, source_count, source_max, timezone, config_hash = found[0]
    return {
        "computed_at": computed_at,
        "source_count": int(source_count),
        "source_max_message_id": source_max,
        "timezone": timezone,
        "config_hash": config_hash,
    }
