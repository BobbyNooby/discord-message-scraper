"""Configuration: secrets from the environment, settings from ``config.toml``.

The rule is one line long — *a secret is an environment variable; anything a
fork might change is a config value*. A token in a committed file is a leaked
token; a timezone in a secret store is an operator setting nobody can change.

Nothing here is chat-specific. Every default is neutral, so a fork changes
values rather than code.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from discord_scraper.scraper.config import BATCH_SIZE, PROJECT_DIR, normalize_batch_size
from discord_scraper.scraper.config import Config as ScraperConfig

DEFAULT_APP_NAME = "Discord Scraper"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_COUNT_TYPES = (0, 19)  # real messages only: chat, not calls/joins/pins


@dataclass(frozen=True)
class Secrets:
    """Never from a file in the repository."""

    token: str
    client_secret: str | None = None
    session_secret: str | None = None
    admin_id: str | None = None


@dataclass(frozen=True)
class Settings:
    channel_id: str
    app_name: str = DEFAULT_APP_NAME
    timezone: str = DEFAULT_TIMEZONE
    count_types: tuple[int, ...] = DEFAULT_COUNT_TYPES
    exclude_bots: bool = True
    # Rich media (§11.1, "most reposted"). `thumbnails` is "proxy" (fetched by
    # this server, cached, so a viewer's IP never reaches Tenor or Klipy),
    # "direct" (the browser talks to their CDN) or "off" (numbers only).
    media_top_n: int = 24
    media_thumbnails: str = "proxy"
    batch_size: int = BATCH_SIZE
    stopword_packs: list[str] = field(default_factory=lambda: ["en"])
    stopword_dir: Path = PROJECT_DIR / "stopwords.d"
    ignore_words: list[str] = field(default_factory=list)
    identities: dict[str, list[str]] = field(default_factory=dict)
    cron_enabled: bool = True
    cron_hours: int = 6
    week_start: str = "monday"
    time_format: str = "24h"
    show_recent: int = 5
    allowlist: str = "posted"
    client_id: str = ""
    redirect_uri: str = ""


@dataclass(frozen=True)
class Config:
    secrets: Secrets
    settings: Settings

    # Convenience passthroughs: callers read ``cfg.token``, not the nesting.
    @property
    def token(self) -> str:
        return self.secrets.token

    @property
    def client_secret(self) -> str | None:
        return self.secrets.client_secret

    @property
    def session_secret(self) -> str | None:
        return self.secrets.session_secret

    @property
    def admin_id(self) -> str | None:
        return self.secrets.admin_id

    @property
    def channel_id(self) -> str:
        return self.settings.channel_id

    @property
    def app_name(self) -> str:
        return self.settings.app_name

    @property
    def timezone(self) -> str:
        return self.settings.timezone

    @property
    def count_types(self) -> tuple[int, ...]:
        return self.settings.count_types

    @property
    def exclude_bots(self) -> bool:
        return self.settings.exclude_bots

    @property
    def batch_size(self) -> int:
        return self.settings.batch_size

    @property
    def stopword_packs(self) -> list[str]:
        return self.settings.stopword_packs

    @property
    def stopword_dir(self) -> Path:
        return self.settings.stopword_dir

    @property
    def ignore_words(self) -> list[str]:
        return self.settings.ignore_words

    @property
    def identities(self) -> dict[str, list[str]]:
        return self.settings.identities

    @property
    def cron_enabled(self) -> bool:
        return self.settings.cron_enabled

    @property
    def cron_hours(self) -> int:
        return self.settings.cron_hours

    @property
    def week_start(self) -> str:
        return self.settings.week_start

    @property
    def time_format(self) -> str:
        return self.settings.time_format

    @property
    def media_top_n(self) -> int:
        return self.settings.media_top_n

    @property
    def media_thumbnails(self) -> str:
        return self.settings.media_thumbnails

    @property
    def show_recent(self) -> int:
        return self.settings.show_recent

    @property
    def allowlist(self) -> str:
        return self.settings.allowlist

    @property
    def client_id(self) -> str:
        return self.settings.client_id

    @property
    def redirect_uri(self) -> str:
        return self.settings.redirect_uri

    def scraper(self) -> ScraperConfig:
        """The three fields the scrape pipeline actually needs."""
        return ScraperConfig(
            token=self.token, channel_id=self.channel_id, batch_size=self.batch_size
        )


def _load_env_file(env_path: Path) -> dict[str, str]:
    """Real environment wins; the file only fills gaps."""
    env = dict(os.environ)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            env.setdefault(key.strip(), value.strip())
    return env


def _secrets_from(env: dict[str, str], *, require_token: bool = True) -> Secrets:
    token = env.get("DISCORD_TOKEN")
    if not token and require_token:
        raise SystemExit(
            "Missing DISCORD_TOKEN. Put it in .env or the environment — "
            "secrets never live in config.toml."
        )
    return Secrets(
        token=token or "",
        client_secret=env.get("DISCORD_CLIENT_SECRET") or None,
        session_secret=env.get("SESSION_SECRET") or None,
        admin_id=env.get("ADMIN_DISCORD_ID") or None,
    )


def _settings_from(raw: dict[str, Any], root: Path) -> Settings:
    app = raw.get("app", {})
    source = raw.get("source", {})
    stop = raw.get("stopwords", {})
    worker = raw.get("worker", {})
    ui = raw.get("ui", {})
    access = raw.get("access", {})
    media = raw.get("media", {})

    channel_id = str(source.get("channel_id") or "").strip()
    if not channel_id:
        raise SystemExit("Missing source.channel_id in config.toml.")

    packs = stop.get("packs")
    stop_dir = stop.get("dir")

    return Settings(
        channel_id=channel_id,
        app_name=str(app.get("name", DEFAULT_APP_NAME)),
        timezone=str(source.get("timezone", DEFAULT_TIMEZONE)),
        count_types=tuple(source.get("count_types", DEFAULT_COUNT_TYPES)),
        exclude_bots=bool(source.get("exclude_bots", True)),
        batch_size=normalize_batch_size(source.get("batch_size", BATCH_SIZE)),
        stopword_packs=[str(p) for p in packs] if packs else ["en"],
        stopword_dir=Path(stop_dir).expanduser() if stop_dir else root / "stopwords.d",
        ignore_words=[str(w) for w in stop.get("ignore", [])],
        identities={str(k): [str(a) for a in v]
                    for k, v in raw.get("identity", {}).items()},
        cron_enabled=bool(worker.get("cron_enabled", True)),
        cron_hours=int(worker.get("cron_hours", 6)),
        week_start=str(ui.get("week_start", "monday")),
        time_format=str(ui.get("time_format", "24h")),
        show_recent=int(ui.get("show_recent", 5)),
        media_top_n=max(0, int(media.get("top_n", 24))),
        media_thumbnails=str(media.get("thumbnails", "proxy")),
        allowlist=str(access.get("allowlist", "posted")),
        # Not secrets: a client id and a redirect URI differ per deployment and
        # are public by design, so they belong in the file a fork edits.
        client_id=str(access.get("client_id", "")),
        redirect_uri=str(access.get("redirect_uri", "")),
    )


def load_config(
    env_path: str | Path | None = None,
    config_path: str | Path | None = None,
    *,
    require_token: bool = True,
) -> Config:
    """Everything the app needs.

    ``require_token=False`` is for the API-only shape: it still reads every other
    secret from the environment (the OAuth client secret, the admin id), it just
    does not insist on a Discord token that shape never uses.
    """
    env_file = Path(env_path) if env_path is not None else PROJECT_DIR / ".env"
    toml_file = Path(config_path) if config_path is not None else PROJECT_DIR / "config.toml"

    env = _load_env_file(env_file)
    raw = tomllib.loads(toml_file.read_text(encoding="utf-8")) if toml_file.exists() else {}

    return Config(
        secrets=_secrets_from(env, require_token=require_token),
        settings=_settings_from(raw, toml_file.parent),
    )


def load_settings(config_path: str | Path | None = None) -> Settings:
    """Settings only, no secrets.

    A rollup rebuild reads the channel, timezone, stopword and identity settings
    and never touches Discord, so it must not require ``DISCORD_TOKEN`` to be
    present — demanding a credential for offline work would make the CLI
    unusable on a machine that only reads an existing database.
    """
    toml_file = Path(config_path) if config_path is not None else PROJECT_DIR / "config.toml"
    if not toml_file.exists():
        raise SystemExit(
            f"No config.toml at {toml_file}. Copy config.example.toml there and set "
            "source.channel_id."
        )
    raw = tomllib.loads(toml_file.read_text(encoding="utf-8"))
    return _settings_from(raw, toml_file.parent)


def config_from_settings(settings: Settings) -> Config:
    """A ``Config`` with no secrets, for code paths that never call Discord."""
    return Config(secrets=Secrets(token=""), settings=settings)


_CACHE: dict[tuple[str, str], Config] = {}


def get_config(
    env_path: str | Path | None = None,
    config_path: str | Path | None = None,
) -> Config:
    """Cached ``load_config`` — reading the environment on every request would
    be wasteful, and the values do not change while the process runs."""
    env_file = Path(env_path) if env_path is not None else PROJECT_DIR / ".env"
    toml_file = Path(config_path) if config_path is not None else PROJECT_DIR / "config.toml"
    key = (str(env_file), str(toml_file))
    if key not in _CACHE:
        _CACHE[key] = load_config(env_file, toml_file)
    return _CACHE[key]
