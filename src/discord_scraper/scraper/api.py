"""Thin Discord REST client (stdlib urllib only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from discord_scraper.scraper.config import API_BASE, BATCH_SIZE, USER_AGENT


def _http_error(code: int, reason: str, what: str) -> str:
    """A readable sentence for an HTTP failure, naming what was being read.

    One mapper for every caller, so a 401 says the same thing whether it came
    from the message feed or an admin's on-demand token check.
    """
    if code == 401:
        return "HTTP 401: token is invalid or revoked"
    if code == 403:
        return f"HTTP 403: no permission to read {what}"
    if code == 404:
        return f"HTTP 404: {what} not found"
    return f"HTTP {code} {reason}: {what}"


def get_json(
    url: str, token: str, *, timeout: float = 10.0
) -> tuple[int | None, Any, str | None]:
    """One authorized GET, returning ``(status, body, error)``.

    Unlike :func:`fetch_messages` this never raises: an unreachable Discord or a
    refusal is a result the caller reports, not an exception that turns an admin
    button into a 500. ``status`` is ``None`` when nothing answered at all.
    """
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": token,
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode()
            return int(response.status), (json.loads(raw) if raw else None), None
    except urllib.error.HTTPError as exc:
        what = urllib.parse.urlparse(url).path
        return int(exc.code), None, _http_error(int(exc.code), str(exc.reason), what)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, None, f"network error: {reason}"
    except ValueError as exc:
        return None, None, f"unreadable response: {exc}"


def get_current_user(token: str, *, timeout: float = 10.0) -> tuple[int | None, Any, str | None]:
    """``GET /users/@me`` — the only endpoint that proves a token still works."""
    return get_json(f"{API_BASE}/users/@me", token, timeout=timeout)


def get_channel(
    token: str, channel_id: str, *, timeout: float = 10.0
) -> tuple[int | None, Any, str | None]:
    """``GET /channels/{id}`` — proves the bot can actually reach the channel."""
    return get_json(f"{API_BASE}/channels/{channel_id}", token, timeout=timeout)


def fetch_messages(token: str, channel_id: str, before: str | None, limit: int = BATCH_SIZE) -> list[dict]:
    """Fetch one batch of messages, newest-first. Empty list = end of history.

    `before` is an exclusive upper bound on message snowflake; pagination walks
    history by feeding back the oldest id of each batch. Retries transparently
    on HTTP 429 using the server's retry_after.
    """
    params = {"limit": str(limit)}
    if before:
        params["before"] = before
    url = f"{API_BASE}/channels/{channel_id}/messages?{urllib.parse.urlencode(params)}"

    while True:
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": token,
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = 1.0
                try:
                    retry_after = float(
                        json.loads(e.read().decode()).get("retry_after", 1.0)
                    )
                except Exception:
                    pass
                wait = retry_after + 0.25
                print(f"  rate limited, waiting {wait:.2f}s...")
                time.sleep(wait)
                continue
            if e.code in (401, 403):
                raise RuntimeError("HTTP 401/403: token invalid or channel not accessible")
            if e.code == 404:
                raise RuntimeError(f"HTTP 404: channel {channel_id} not found")
            raise RuntimeError(f"HTTP {e.code} {e.reason}: {url}")
