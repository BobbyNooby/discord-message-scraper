"""Who is who: display names, bot exclusion, and alt merging.

Merging happens here, before aggregation, so a merged person is one row in every
rollup and nothing downstream needs to know identity exists (spec §5.5).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from discord_scraper.config import Config


@dataclass(frozen=True)
class Identities:
    """The authors of one channel, as rows the rollups can index."""

    index: dict[str, int]            # author_id -> row
    names: list[str]                 # display name per row
    accounts: list[list[str]]        # every raw account folded into the row
    groups: list[tuple[str, list[str]]]   # what merging actually did
    canonical: dict[str, str]        # every account -> its canonical id


def latest_display_names(conn: sqlite3.Connection) -> dict[str, str]:
    """``author_id -> name`` from that author's most recent message.

    The most recent message wins, never ``MAX()`` over the column (spec §5.8):
    people rename themselves, and ``MAX`` would pick the alphabetically loudest
    name they have ever used rather than the one they use now.
    """
    rows = conn.execute(
        """
        WITH ranked AS (
            SELECT author_id,
                   json_extract(raw, '$.author.global_name') AS global_name,
                   COALESCE(json_extract(raw, '$.author.username'), author_username) AS username,
                   ROW_NUMBER() OVER (PARTITION BY author_id
                                      ORDER BY created_at DESC, id DESC) AS rn
              FROM messages
        )
        SELECT author_id, global_name, username FROM ranked WHERE rn = 1
        """
    ).fetchall()
    return {
        author_id: global_name or username or author_id
        for author_id, global_name, username in rows
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _merge_map(conn: sqlite3.Connection, cfg: Config) -> dict[str, str]:
    """``account_id -> canonical_id``, from config.toml *and* the identity table.

    Both are read: the file is the portable form (it travels with a fork), the
    table is what the admin page writes. The table wins where they disagree,
    because it is the newer of the two.
    """
    merge: dict[str, str] = {}
    for canonical, alts in (cfg.identities or {}).items():
        merge[str(canonical)] = str(canonical)
        for alt in alts:
            merge[str(alt)] = str(canonical)
    if _table_exists(conn, "identity"):
        for canonical, account in conn.execute(
            "SELECT canonical_id, account_id FROM identity WHERE channel_id = ?",
            (cfg.channel_id,),
        ):
            merge[str(account)] = str(canonical)
    return merge


def _canonical(author_id: str, merge: dict[str, str]) -> str:
    """Follow a merge chain (a merged alt may itself be a canonical elsewhere)."""
    seen = {author_id}
    current = merge.get(author_id, author_id)
    while current != author_id and current not in seen:
        seen.add(current)
        author_id, current = current, merge.get(current, current)
    return current


def counted_scope(cfg: Config) -> tuple[str, list[object]]:
    """The WHERE fragment and params selecting the messages rollups count.

    One definition shared by identity and the build keeps "what counts" from
    drifting into two answers: this channel, the configured real types, and no
    bots.
    """
    where = ["channel_id = ?"]
    params: list[object] = [cfg.channel_id]
    types = tuple(cfg.count_types)
    if types:
        where.append(f"type IN ({','.join('?' for _ in types)})")
        params.extend(types)
    if cfg.exclude_bots:
        # The field is absent — not false — for humans, so only `= 1` is a bot.
        where.append("(json_extract(raw, '$.author.bot') IS NULL "
                     "OR json_extract(raw, '$.author.bot') != 1)")
    return " AND ".join(where), params


def _hidden(conn: sqlite3.Connection, cfg: Config) -> set[str]:
    """Canonical ids an operator switched off in the admin board (§5.6)."""
    if not _table_exists(conn, "author_names"):
        return set()
    return {
        row[0] for row in conn.execute(
            "SELECT author_id FROM author_names WHERE channel_id = ? AND included = 0",
            (cfg.channel_id,),
        )
    }


def _author_counts(conn: sqlite3.Connection, cfg: Config) -> list[tuple[str, int]]:
    """Counted messages per author: the channel's real messages, bots excluded."""
    where, params = counted_scope(cfg)
    return [
        (author_id, count)
        for author_id, count in conn.execute(
            f"""
            SELECT author_id, COUNT(*) AS n
              FROM messages
             WHERE {where}
             GROUP BY author_id
            """,
            params,
        )
    ]


def resolve_identities(conn: sqlite3.Connection, cfg: Config) -> Identities:
    """The channel's authors as displayable rows, alts folded together."""
    display = latest_display_names(conn)
    merge = _merge_map(conn, cfg)
    hidden = _hidden(conn, cfg)

    members: dict[str, list[str]] = {}
    totals: dict[str, int] = {}
    for author_id, count in _author_counts(conn, cfg):
        canonical = _canonical(author_id, merge)
        if canonical in hidden:
            continue
        members.setdefault(canonical, []).append(author_id)
        totals[canonical] = totals.get(canonical, 0) + count

    ordered = sorted(members, key=lambda who: (-totals[who], who))
    # Every raw account resolves to its row, not only the canonical: a rollup
    # lookup receives whichever account actually sent the message.
    index = {
        account: row
        for row, canonical in enumerate(ordered)
        for account in members[canonical]
    }
    names = [display.get(canonical) or canonical for canonical in ordered]
    accounts = [sorted(members[canonical]) for canonical in ordered]
    canonical_of = {account: canonical for canonical in ordered for account in members[canonical]}
    groups = [
        (canonical, sorted(members[canonical]))
        for canonical in ordered
        if len(members[canonical]) > 1
    ]
    return Identities(index=index, names=names, accounts=accounts, groups=groups,
                      canonical=canonical_of)
