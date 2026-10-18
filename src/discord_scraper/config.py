"""Configuration: secrets from the environment, settings from ``config.toml``.

The rule is one line long — *a secret is an environment variable; anything a
fork might change is a config value*. A token in a committed file is a leaked
token; a timezone in a secret store is an operator setting nobody can change.

Nothing here is chat-specific. Every default is neutral, so a fork changes
values rather than code.
"""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord_scraper.scraper.config import BATCH_SIZE, PROJECT_DIR, normalize_batch_size

DEFAULT_APP_NAME = "Discord Scraper"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_COUNT_TYPES = (0, 19)  # real messages only: chat, not calls/joins/pins

# "auto" means the host zone (see ``timeutil.resolve_tz``); everything else has
# to be a name ``ZoneInfo`` knows, or every date bucket silently lands wrong.
AUTO_TIMEZONE = "auto"
MAX_SHOW_RECENT = 200
MIN_CRON_HOURS = 1
MAX_CRON_HOURS = 24
# Mirrors ``api.admin.ALLOWLIST_POLICIES``; config.py must not import the web
# extra, and these two strings are the contract the auth layer reads.
ALLOWLIST_MODES = ("posted", "explicit")
THUMBNAIL_MODES = ("proxy", "direct", "off")
# ``ui`` values with a closed vocabulary: the example file names the only
# alternative, so anything else is a typo the operator wants to hear about.
WEEK_START_DAYS = ("monday", "sunday")
TIME_FORMATS = ("24h", "12h")


@dataclass(frozen=True)
class Secrets:
    """Never from a file in the repository."""

    token: str
    client_secret: str | None = None
    client_id: str | None = None
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
        # Public by design, so it may live in the config file or the
        # environment. The file wins when both are set.
        return self.settings.client_id or self.secrets.client_id or ""

    @property
    def redirect_uri(self) -> str:
        return self.settings.redirect_uri


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
        client_id=env.get("DISCORD_CLIENT_ID") or None,
        session_secret=env.get("SESSION_SECRET") or None,
        admin_id=env.get("ADMIN_DISCORD_ID") or None,
    )


def is_timezone(name: str) -> bool:
    """True for a real IANA zone, and for the documented ``"auto"``.

    One predicate so the config file and the admin board cannot disagree: the
    board's PUT validator used to answer "unknown timezone: auto" for the value
    the loader documents, which left an operator unable to save what the file
    already allowed.
    """
    if name == AUTO_TIMEZONE:
        return True
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        # A slash-less offset like "GMT+8" can raise either one.
        return False
    return True


def _int_setting(key: str, value: Any) -> int:
    """A whole number, or a config error that names where it came from."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise SystemExit(f'{key}: "{value}" is not a whole number') from None


def _display(value: Any) -> str:
    """A value as it appears in a config error: a string keeps its quotes."""
    return f'"{value}"' if isinstance(value, str) else str(value)


def _bool_setting(key: str, value: Any) -> bool:
    """A real boolean.

    ``bool("no")`` and ``bool("false")`` are both True, so a quoted string in a
    hand-edited file used to mean the opposite of what it said.
    """
    if not isinstance(value, bool):
        raise SystemExit(f"{key}: {_display(value)} is not true or false")
    return value


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

    timezone = str(source.get("timezone", DEFAULT_TIMEZONE))
    if not is_timezone(timezone):
        raise SystemExit(
            f'source.timezone: "{timezone}" is not an IANA timezone name '
            '(try "Asia/Kuala_Lumpur", or "auto" for the host zone)'
        )

    raw_count_types = source.get("count_types", DEFAULT_COUNT_TYPES)
    if not isinstance(raw_count_types, (list, tuple)) or not all(
        isinstance(t, int) and not isinstance(t, bool) for t in raw_count_types
    ):
        raise SystemExit(
            f"source.count_types: {_display(raw_count_types)} is not a list of "
            "message types (for example [0, 19])"
        )
    count_types = tuple(raw_count_types)
    if not count_types:
        raise SystemExit(
            "source.count_types: needs at least one message type (for example [0, 19])"
        )

    exclude_bots = _bool_setting(
        "source.exclude_bots", source.get("exclude_bots", True)
    )

    cron_hours = _int_setting("worker.cron_hours", worker.get("cron_hours", 6))
    if not MIN_CRON_HOURS <= cron_hours <= MAX_CRON_HOURS:
        raise SystemExit(
            f"worker.cron_hours: {cron_hours} is out of range "
            f"({MIN_CRON_HOURS}-{MAX_CRON_HOURS})"
        )

    show_recent = _int_setting("ui.show_recent", ui.get("show_recent", 5))
    if not 0 <= show_recent <= MAX_SHOW_RECENT:
        raise SystemExit(
            f"ui.show_recent: {show_recent} is out of range (0-{MAX_SHOW_RECENT})"
        )

    media_top_n = _int_setting("media.top_n", media.get("top_n", 24))
    if media_top_n < 0:
        raise SystemExit(f"media.top_n: {media_top_n} is out of range (>= 0)")

    media_thumbnails = str(media.get("thumbnails", "proxy"))
    if media_thumbnails not in THUMBNAIL_MODES:
        raise SystemExit(
            f'media.thumbnails: "{media_thumbnails}" is not one of: '
            + ", ".join(THUMBNAIL_MODES)
        )

    allowlist = str(access.get("allowlist", "posted"))
    if allowlist not in ALLOWLIST_MODES:
        raise SystemExit(
            f'access.allowlist: "{allowlist}" is not one of: ' + ", ".join(ALLOWLIST_MODES)
        )

    packs = stop.get("packs", ["en"])
    if not isinstance(packs, (list, tuple)) or not all(
        isinstance(p, str) for p in packs
    ):
        raise SystemExit(
            f"stopwords.packs: {_display(packs)} is not a list of pack names "
            '(for example ["en"])'
        )

    ignore = stop.get("ignore", [])
    if not isinstance(ignore, (list, tuple)) or not all(
        isinstance(w, str) for w in ignore
    ):
        raise SystemExit(
            f"stopwords.ignore: {_display(ignore)} is not a list of words"
        )

    stop_dir = stop.get("dir")
    if stop_dir:
        if not isinstance(stop_dir, str):
            raise SystemExit(f"stopwords.dir: {stop_dir} is not a path")
        # A relative dir belongs to the file it was written in, not to whichever
        # directory the command happened to run from.
        stopword_dir = Path(stop_dir).expanduser()
        if not stopword_dir.is_absolute():
            stopword_dir = root / stopword_dir
        if not stopword_dir.is_dir():
            # Extra word lists are a refinement, not a requirement: a config
            # kept away from the checkout used to die here for a directory it
            # only optionally needed. The packaged packs still load, so say
            # what is missing and carry on.
            print(
                f'warning: stopwords.dir: "{stop_dir}" is not a directory '
                f"({stopword_dir}) — ignoring it and continuing with the "
                "packaged word packs; create the directory to add your own",
                file=sys.stderr,
            )
    else:
        stopword_dir = root / "stopwords.d"

    week_start = str(ui.get("week_start", "monday"))
    if week_start not in WEEK_START_DAYS:
        raise SystemExit(
            f'ui.week_start: "{week_start}" is not one of: ' + ", ".join(WEEK_START_DAYS)
        )

    time_format = str(ui.get("time_format", "24h"))
    if time_format not in TIME_FORMATS:
        raise SystemExit(
            f'ui.time_format: "{time_format}" is not one of: ' + ", ".join(TIME_FORMATS)
        )

    identity_raw = raw.get("identity", {})
    if not isinstance(identity_raw, dict):
        raise SystemExit(f"identity: {_display(identity_raw)} is not a table of names")
    identities: dict[str, list[str]] = {}
    for name, accounts in identity_raw.items():
        if not isinstance(accounts, (list, tuple)) or not all(
            isinstance(a, str) for a in accounts
        ):
            raise SystemExit(
                f"identity.{name}: {_display(accounts)} is not a list of account ids"
            )
        identities[str(name)] = [str(a) for a in accounts]

    cron_enabled = _bool_setting(
        "worker.cron_enabled", worker.get("cron_enabled", True)
    )

    return Settings(
        channel_id=channel_id,
        app_name=str(app.get("name", DEFAULT_APP_NAME)),
        timezone=timezone,
        count_types=count_types,
        exclude_bots=exclude_bots,
        batch_size=normalize_batch_size(source.get("batch_size", BATCH_SIZE)),
        stopword_packs=list(packs) if packs else ["en"],
        stopword_dir=stopword_dir,
        ignore_words=list(ignore),
        identities=identities,
        cron_enabled=cron_enabled,
        cron_hours=cron_hours,
        week_start=week_start,
        time_format=time_format,
        show_recent=show_recent,
        media_top_n=media_top_n,
        media_thumbnails=media_thumbnails,
        allowlist=allowlist,
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
