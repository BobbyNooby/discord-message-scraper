"""The token probe — the only way to learn that a user token is dead.

Discord user tokens carry no expiry and no endpoint reports a lifetime, so
`GET /api/v9/users/@me` is the entire detector (spec §10.1). The codes mean
different things and are kept apart here:

* ``200`` the token works
* ``401`` the token is dead — rotate it
* ``403`` the token is fine, the channel is not reachable — fix the config
* anything else (including a network error) leaves the status *unknown* rather
  than guessing, because a wrong "dead" sends someone rotating a working token
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Callable

import httpx

from discord_scraper.worker.state import set_worker_state

ME_URL = "https://discord.com/api/v9/users/@me"
TIMEOUT = 15.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def probe_token(
    conn: sqlite3.Connection,
    token: str,
    *,
    get: Callable[..., object] | None = None,
) -> tuple[bool, int]:
    """Ask Discord who this token is. Returns ``(alive, status_code)``.

    ``get`` exists so the probe can be tested without a Discord account; every
    other caller leaves it alone.
    """
    request = get or httpx.get
    try:
        response = request(
            ME_URL,
            headers={"Authorization": token},
            timeout=TIMEOUT,
        )
        status = int(getattr(response, "status_code", 0))
    except Exception:
        status = 0

    if status == 200:
        set_worker_state(conn, "token_status", "ok")
        set_worker_state(conn, "token_last_ok_at", _now())
    elif status == 401:
        set_worker_state(conn, "token_status", "dead")
    elif status == 403:
        # The token authenticated; the channel is the problem. Never downgrade a
        # known-dead token on a 403, and never call a live one dead either.
        if conn.execute(
            "SELECT 1 FROM worker_state WHERE key = 'token_status' AND value = 'dead'"
        ).fetchone() is None:
            set_worker_state(conn, "token_status", "ok")
    else:
        set_worker_state(conn, "token_status", "unknown")
    set_worker_state(conn, "token_checked_at", _now())
    return status == 200, status
