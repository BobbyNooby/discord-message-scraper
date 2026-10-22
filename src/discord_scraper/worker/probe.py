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

from discord_scraper.scraper.api import get_current_user
from discord_scraper.worker.state import set_worker_state

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
    other caller leaves it alone and goes through the stdlib client, because the
    tool has no dependencies to lean on.
    """
    if get is None:
        answered, _body, _error = get_current_user(token, timeout=TIMEOUT)
        status = int(answered or 0)
    else:
        try:
            response = get(token=token, timeout=TIMEOUT)
            status = int(getattr(response, "status_code", 0))
        except Exception:
            status = 0

    if status == 200:
        set_worker_state(conn, "token_status", "ok")
        set_worker_state(conn, "token_last_ok_at", _now())
    elif status == 401:
        set_worker_state(conn, "token_status", "dead")
    else:
        known_dead = conn.execute(
            "SELECT 1 FROM worker_state WHERE key = 'token_status' AND value = 'dead'"
        ).fetchone() is not None
        # Neither a 403 nor a failed request says anything about a token already
        # known dead, so it stays dead: downgrading it to "unknown" lifted the
        # pause and spent a scheduled scrape on a token Discord had refused.
        if not known_dead:
            # A 403 means the token authenticated and the channel is the problem.
            set_worker_state(conn, "token_status", "ok" if status == 403 else "unknown")
    set_worker_state(conn, "token_checked_at", _now())
    return status == 200, status
