"""The text pipeline: raw Discord message text to analysis tokens.

Order matters and is fixed by spec §6.1 — a URL removed after markdown
stripping would already have lost its ``://``, and code removed after
tokenizing would have polluted the word counts it was meant to stay out of.
"""

from __future__ import annotations

import re
from pathlib import Path

DATA_DIR = Path(__file__).with_name("data")

# --- stage patterns, applied in this order -----------------------------------

_FENCE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE = re.compile(r"`[^`]*`")
_URL = re.compile(
    r"https?://\S+"
    r"|www\.\S+"
    r"|\b\S+\.(?:com|net|org|gg|io|co|be|ly|tv|me|app|dev|xyz|link|to|uk|my|sg)(?:/\S*)?",
    re.IGNORECASE,
)
# Custom emoji keep their name ("kek"); mentions, roles and channels are noise.
_DISCORD = re.compile(r"<a:(\w+):\d+>|<:(\w+):\d+>|<@[!&]?\d+>|<#\d+>")
_EMOJI_RANGES = (
    "\U0001f000-\U0001faff"
    "\U00002190-\U000021ff"
    "\U00002600-\U000027bf"
    "\U0000fe00-\U0000fe0f"
    "\U00002b00-\U00002bff"
)
# The pipeline removes a whole run at once; the emoji rollup counts each glyph,
# so it matches one at a time and ignores the joiners and modifiers that ride
# along with it (variation selectors, skin tones, ZWJ).
_EMOJI = re.compile(f"[{_EMOJI_RANGES}]+")
_ONE_EMOJI = re.compile(f"[{_EMOJI_RANGES}]")
_EMOJI_MODIFIER = re.compile("[\ufe00-\ufe0f\U0001f3fb-\U0001f3ff\u200d]")
_MARKDOWN = re.compile(r"[*_~|>\[\]()]+")
_ELONGATION = re.compile(r"(.)\1{2,}")
_TOKEN = re.compile(r"[a-z]{3,}")


def _entity(match: re.Match) -> str:
    """Keep a custom emoji's name; mentions, roles and channels leave nothing."""
    name = match.group(1) or match.group(2)
    return f" {name} " if name else " "


def clean(text: str) -> str:
    """Return ``text`` reduced to lowercase, entity-free, elongated-clean words."""
    if not text:
        return ""
    out = _FENCE.sub(" ", text)
    out = _INLINE_CODE.sub(" ", out)
    out = _URL.sub(" ", out)
    out = _DISCORD.sub(_entity, out)
    out = _EMOJI.sub(" ", out)
    out = _MARKDOWN.sub(" ", out)
    out = out.lower()
    out = _ELONGATION.sub(r"\1\1", out)
    return " ".join(out.split())


def tokens(text: str) -> list[str]:
    """Words worth counting: three or more letters, no digits."""
    return _TOKEN.findall(clean(text))


def links(text: str) -> list[str]:
    """URLs as they appeared, in order — the raw material of link rollups."""
    return _URL.findall(text or "")


def emoji(text: str) -> list[str]:
    """Unicode emoji, one entry per glyph. The word pipeline drops these; the
    emoji rollup is the only place they survive, so extraction lives beside the
    strip rather than being re-derived there."""
    return [glyph for glyph in _ONE_EMOJI.findall(text or "")
            if not _EMOJI_MODIFIER.match(glyph)]


def _read_words(path: Path) -> set[str]:
    """One word per line; blank lines and ``#`` comments ignored."""
    if not path.is_file():
        return set()
    words = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        word = line.strip().lower()
        if word and not word.startswith("#"):
            words.add(word)
    return words


def load_stopwords(cfg) -> frozenset[str]:
    """English corpus + chat filler, then any named packs, then user lists.

    User lists are merged last so the operator always wins (spec §6.2).
    """
    words = _read_words(DATA_DIR / "stopwords-en.txt")
    words |= _read_words(DATA_DIR / "stopwords-chat.txt")
    for pack in cfg.stopword_packs:
        words |= _read_words(DATA_DIR / f"stopwords-{pack}.txt")
    if cfg.stopword_dir.is_dir():
        for path in sorted(cfg.stopword_dir.glob("*.txt")):
            words |= _read_words(path)
    words |= {w.strip().lower() for w in cfg.ignore_words if w.strip()}
    return frozenset(words)
