"""Read helpers over the rollup tables.

Every function here reads only ``rollup_*``. That is the whole point of the
design (spec §5.3): the API assembles its payload from precomputed grains, so a
request never scans ``messages`` — and the 740 MB of raw JSON is never in the
read path.
"""

from __future__ import annotations

import sqlite3
from typing import Any

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


def authors(conn: sqlite3.Connection, channel_id: str) -> list[dict[str, Any]]:
    """Authors in the same order as every ``author_id``-keyed rollup's index.

    Ordered by message volume, then id, because the frontend colours series by
    position and a rebuild must not reshuffle the palette. Names come from
    ``author_names`` — never from ``messages``, which a request must not scan.
    """
    totals: dict[str, int] = {}
    for author_id, msgs in rows(
        conn,
        """
        SELECT author_id, SUM(msgs) AS msgs FROM rollup_activity
         WHERE channel_id = ? GROUP BY author_id
        """,
        (channel_id,),
    ):
        totals[author_id] = int(msgs)
    names = dict(rows(
        conn, "SELECT author_id, name FROM author_names WHERE channel_id = ?", (channel_id,)
    ))
    members: dict[str, list[str]] = {}
    for account, canonical in rows(
        conn,
        "SELECT account_id, canonical_id FROM identity WHERE channel_id = ? ORDER BY account_id",
        (channel_id,),
    ):
        members.setdefault(canonical, []).append(account)
    ordered = sorted(totals, key=lambda who: (-totals[who], who))
    return [
        {
            "id": who,
            "name": names.get(who, who),
            "msgs": totals[who],
            "accounts": [who, *[a for a in members.get(who, []) if a != who]],
        }
        for who in ordered
    ]


def activity_cells(conn, channel_id: str, day_index: dict[str, int], author_index: dict[str, int]):
    return [
        [day_index[day], author_index[author_id], msgs, chars, replies, media, edits]
        for day, author_id, msgs, chars, media, edits, replies in rows(
            conn,
            """
            SELECT day, author_id, msgs, chars, media, edits, replies
              FROM rollup_activity WHERE channel_id = ? ORDER BY day, author_id
            """,
            (channel_id,),
        )
        if day in day_index and author_id in author_index
    ]


def heat_cells(conn, channel_id: str, author_index: dict[str, int]):
    output = []
    for year, dow, hour, author_id, msgs in rows(
        conn,
        """
        SELECT year, dow, hour, author_id, msgs FROM rollup_heat
         WHERE channel_id = ? ORDER BY year, dow, hour, author_id
        """,
        (channel_id,),
    ):
        if author_id in author_index:
            output.append([dow, hour, author_index[author_id], year, msgs])
    return output


def reply_edges(conn, channel_id: str, author_index: dict[str, int]):
    return [
        [author_index[parent], author_index[child], msgs]
        for parent, child, msgs in rows(
            conn,
            """
            SELECT parent_author_id, child_author_id, msgs FROM rollup_reply
             WHERE channel_id = ? ORDER BY parent_author_id, child_author_id
            """,
            (channel_id,),
        )
        if parent in author_index and child in author_index
    ]


def response_times(conn, channel_id: str, author_index: dict[str, int]):
    output = []
    for author_id, replies, seconds, capped in rows(
        conn,
        """
        SELECT author_id, replies, total_seconds, capped_replies FROM rollup_response
         WHERE channel_id = ? ORDER BY author_id
        """,
        (channel_id,),
    ):
        if author_id in author_index and replies:
            output.append([author_index[author_id], round(seconds / replies, 1), replies])
    return output


def edit_speeds(conn, channel_id: str, author_index: dict[str, int]):
    output = []
    for author_id, edits, seconds, capped in rows(
        conn,
        """
        SELECT author_id, edits, total_seconds, capped_edits FROM rollup_edit_speed
         WHERE channel_id = ? ORDER BY author_id
        """,
        (channel_id,),
    ):
        if author_id in author_index and edits:
            output.append([author_index[author_id], round(seconds / edits, 1), edits])
    return output


def starters(conn, channel_id: str, author_index: dict[str, int]):
    return [
        [author_index[author_id], count]
        for author_id, count in rows(
            conn,
            "SELECT author_id, starters FROM rollup_starter WHERE channel_id = ? ORDER BY author_id",
            (channel_id,),
        )
        if author_id in author_index
    ]


def word_rows(conn, channel_id: str) -> list[tuple[str, int, str, int]]:
    return rows(
        conn,
        """
        SELECT word, SUM(uses), author_id, SUM(uses) FROM rollup_word
         WHERE channel_id = ? GROUP BY word, author_id ORDER BY word, author_id
        """,
        (channel_id,),
    )


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
    return rows(
        conn,
        """
        SELECT author_id, SUM(uses) FROM rollup_word
         WHERE channel_id = ? AND word = ? GROUP BY author_id ORDER BY SUM(uses) DESC
        """,
        (channel_id, word),
    )


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
    output = []
    for day, author_id, key, uses in rows(
        conn,
        """
        SELECT day, author_id, domain, uses FROM rollup_link
         WHERE channel_id = ? ORDER BY day, author_id, domain
        """,
        (channel_id,),
    ):
        host = key.split("/", 1)[0]
        if day in day_index and author_id in author_index and host in host_index:
            output.append([day_index[day], author_index[author_id], host_index[host], uses])
    return output


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


def top_media(conn, channel_id: str, limit: int = 24) -> list[list]:
    """``[[url, provider, kind, title, thumbnail, uses], ...]``, most reposted first."""
    return [
        [str(url), str(provider), str(kind), str(title or ""), str(thumbnail or ""), int(uses or 0)]
        for url, provider, kind, title, thumbnail, uses in rows(
            conn,
            """
            SELECT url, provider, kind, title, thumbnail, uses FROM rollup_media
             WHERE channel_id = ? ORDER BY uses DESC, url LIMIT ?
            """,
            (channel_id, limit),
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
