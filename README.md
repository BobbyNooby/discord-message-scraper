# discord-message-scraper

Scrapes the full message history of one Discord DM/group channel into a local
SQLite database for later analysis.

**Python 3.10+, stdlib only** — no pip, no venv, no dependencies for scraping
and the text-based analysis. Two optional extras (not stdlib), installed only
if you want them:

- `python3 -m pip install --user matplotlib` — report charts (`.png`)
- `python3 -m pip install --user pandas` — `MessageDB.frame()` DataFrames

> **ToS caveat:** this uses a user-account token against the private REST API,
> which violates Discord's Terms of Service ("self-botting") and can result in
> account action — use an alt account, throttle conservatively, and accept the
> risk. The compliant alternative is Discord's built-in data export
> (Settings → Privacy & Safety → Request Data).

## Layout

```
main.py               entry point (orchestration + CLI)
analysis.py           run script: default report (pies + timelines)
analysis/
  __init__.py         public API re-exports (`from analysis import MessageDB`)
  __main__.py         `python3 -m analysis` entry point
  cli.py              argparse wiring for `python3 -m analysis`
  db.py               read-only connection + query helpers
  messagedb.py        MessageDB wrapper class
  queries.py          ready-made analyses (top_authors, activity, ...)
  report.py           text report + matplotlib charts
scraper/
  api.py              Discord REST client (urllib, honors 429 retry_after)
  config.py           constants, paths, .env loading
  database.py         schema, resume meta, batch inserts, stats
  transform.py        API message object -> DB row
output/
  messages.db         the database (created on first run)
sql/
  analysis.sql        starter queries for analysis
tests/
  test_catch_up.py    resume behaviour, against a temp DB and a fake API
.env                  DISCORD_TOKEN + CHANNEL_ID (gitignored)
```

## Setup

```sh
cp .env.example .env   # then fill in DISCORD_TOKEN and CHANNEL_ID
```

- `DISCORD_TOKEN` — a **user** token (bots can't read DMs they weren't added to)
- `CHANNEL_ID` — the DM/group channel ID (visible in the browser client URL:
  `/channels/@me/<id>`)

## Run

```sh
python3 main.py                    # full scrape
python3 main.py --max-batches 3    # quick test run (3 batches of 50)
python3 main.py --batch-size 25    # gentler on the API (1-100; default 50)
```

Safe to interrupt (Ctrl-C) and re-run at any time:

- `INSERT OR IGNORE` on message ID — no duplicate rows
- resume points persisted in a `meta` table: `oldest_id` for the older end,
  and `catchup_cursor` for the bottom of a hole left by a catch-up pass that
  was killed part-way. The cursor is what makes an interrupted *update*
  recoverable: the next run skips down past the stripe it already wrote and
  keeps going into the hole, instead of stopping at the first batch it
  recognises and leaving the hole behind.
- newest-first "catch-up" pass grabs messages newer than the DB, then a
  "backfill" pass continues older history from the resume point
- 50 messages per request by default — tune with `--batch-size` or `BATCH_SIZE`
  in `.env` (1–100; Discord's hard max is 100, default 50 keeps you
  conservative), ~0.8s between requests; honors `retry_after` on HTTP 429

Live progress per batch shows cumulative count, elapsed time, current rate,
and DB size:

```
  backfill: 100 fetched, oldest now 15417... | 2s elapsed | 57.6 msg/s | db: 1,300 | ███████░░░░░░░░░░░░░░░░░ ~32% span
```

There is deliberately no message-count "% complete / ETA": Discord's API does
not expose a channel's total message count. The backfill bar instead shows how
much of the channel's **time span** your history covers, from channel creation
(decoded from the channel-id snowflake) to the newest message. It is only a
rough proxy for message progress — quiet months move the bar quickly, busy
months slowly — and it hovers below 100% until the first message is reached.

## Tests

```sh
python3 -m unittest discover -s tests -t .
```

Stdlib `unittest`, no dependencies, no network. The catch-up tests run against
a temp SQLite file with only the HTTP call faked, and cover the resume paths:
a pass killed mid-update, a re-run that is already current, a `--max-batches`
stop, and new messages arriving while a hole is still open.

## Schema

```sql
CREATE TABLE messages (
    id TEXT PRIMARY KEY,           -- Discord snowflake as TEXT (64-bit)
    channel_id TEXT NOT NULL,
    author_id TEXT NOT NULL,
    author_username TEXT,
    content TEXT,
    created_at TEXT NOT NULL,      -- ISO8601
    edited_at TEXT,
    reply_to_id TEXT,
    has_attachments INTEGER DEFAULT 0,
    raw TEXT                       -- full original message JSON
);
CREATE INDEX idx_created ON messages (created_at);
CREATE INDEX idx_author ON messages (author_id);
```

The `raw` column keeps the untouched API payload, so reactions/embeds/mentions
can be extracted later without re-scraping.

## Analysis

Two layers: a no-coding CLI and a Python wrapper for developing your own analysis.

### CLI

```sh
python3 analysis.py                                 # default report: charts + text dump
python3 -m analysis report --bucket auto            # same, auto bucket from data span
python3 -m analysis stats
python3 -m analysis activity --by month            # or day / hour
python3 -m analysis top-authors --limit 10
python3 -m analysis reactions                      # emoji leaderboard (raw JSON)
python3 -m analysis attachments                    # file-type breakdown (raw JSON)
python3 -m analysis mentions                       # most-@'d people (raw JSON)
python3 -m analysis most-replied
python3 -m analysis search "some text"
python3 -m analysis top-words --author exampleuser        # word frequency
python3 -m analysis emoji                          # emoji used in message text
```

Running `analysis.py` (or `python3 -m analysis`) with no command generates `output/report/` in both
**words** and **messages** modes:

- `words_pie.png` / `messages_pie.png` — share per user
- `words_over_time.png` / `messages_over_time.png` — per day/month/year,
  bold TOTAL line + one colored line per user
- `report.txt` — the raw numbers behind all four charts

Charts need matplotlib (`python3 -m pip install --user matplotlib`); the text
report works without it.

Ready-made SQL also lives in `sql/analysis.sql`:

```sh
sqlite3 output/messages.db < sql/analysis.sql
```

### As a library

`MessageDB` wraps the DB so you write plain Python, not SQL + `json.loads`:

```python
from analysis import MessageDB

db = MessageDB()

db.messages()                       # all messages as parsed dicts (raw JSON parsed for you)
db.messages(where="author_username = ?", params=("exampleuser",))
db.query("SELECT ... FROM messages ...")
db.scalar("SELECT COUNT(*) FROM messages")
db.top_authors(10)                  # ready-made analyses are methods too
db.reactions()

df = db.frame()                     # pandas DataFrame (pip install pandas)
```

Every function returns `sqlite3.Row` rows (or plain dicts for `.messages()`),
so results feed straight into `pandas.DataFrame(...)`, `collections.Counter`, etc.


### Querying `raw` (nested JSON)

`raw` holds the original API message object as `json.dumps` output — nested
objects and arrays included. Query it with SQLite's JSON functions:

```sql
-- reaction leaderboard
SELECT json_extract(r.value, '$.emoji.name') AS emoji,
       SUM(json_extract(r.value, '$.count')) AS uses
FROM messages, json_each(messages.raw, '$.reactions') r
GROUP BY emoji ORDER BY uses DESC;

-- attachment types
SELECT json_extract(j.value, '$.content_type') AS type, COUNT(*) AS n
FROM messages, json_each(messages.raw, '$.attachments') j
GROUP BY type ORDER BY n DESC;
```

The shape is Discord's [message object](https://discord.com/developers/docs/resources/message#message-object):

```jsonc
{
  "id": "123456789012345678",           // snowflake, always a string
  "type": 0,                             // 0 default, 19 reply, 3 call, 7 join, ...
  "channel_id": "234567890123456789",
  "author": { "id": "...", "username": "...", "global_name": "..." },
  "content": "message text",             // "" on calls/system/attachment-only
  "timestamp": "2026-09-01T14:46:48.691000+00:00",
  "edited_timestamp": null,              // ISO string once edited
  "pinned": false,
  "mentions": [],                        // arrays live only in raw
  "attachments": [ { "content_type": "image/png", "url": "..." } ],
  "embeds": [],
  "reactions": [ { "emoji": { "name": "🔥" }, "count": 2 } ],
  "message_reference": { "message_id": "..." },      // present on replies
  "referenced_message": { "...full parent message..." } // absent if parent deleted
}
```

If a JSON field becomes central to your analysis, promote it to a real indexed
column — one pass, no re-scrape:

```sql
ALTER TABLE messages ADD COLUMN sticker_count INTEGER;
UPDATE messages
SET sticker_count = json_array_length(raw, '$.sticker_items')
WHERE json_extract(raw, '$.sticker_items') IS NOT NULL;
```

## The stats site (backend)

The repository now also contains the backend for a read-only stats site: the
same scrape, plus precomputed rollup tables and an authenticated API that serves
them. Nothing in this section is needed to scrape or to use the analysis CLI.

### Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[web]"          # the base install needs no dependencies at all
```

Tier 1 — `discord-scraper scrape`, `discord-scraper analyze` — speaks HTTP with
`urllib` and imports nothing outside the standard library. The `web` extra adds
FastAPI, uvicorn, httpx, itsdangerous and APScheduler for the API and worker.

### Configure

Two files, one rule: **a secret is an environment variable, anything a fork
might change is a config value.**

| Where | What |
|---|---|
| `.env` (gitignored) | `DISCORD_TOKEN`, `DISCORD_CLIENT_SECRET`, `SESSION_SECRET`, `ADMIN_DISCORD_ID` |
| `config.toml` (gitignored; copy `config.example.toml`) | channel id, timezone, bot rule, stopword packs, identity merges, OAuth client id and redirect URI |
| `stopwords.d/` | your own `*.txt` lists, one word per line, merged last |

`config.toml` is gitignored because it holds the channel id, which identifies a
private conversation. Defaults are deliberately neutral: timezone `UTC`, no
regional stopword packs, product name "Discord Scraper".

### Run

```bash
discord-scraper scrape                 # or: python3 main.py
discord-scraper analyze stats          # or: python3 -m analysis stats
discord-scraper rollups rebuild        # build every rollup family
discord-scraper rollups status         # row counts and current/stale per family
discord-scraper serve                  # API + ingest worker on 127.0.0.1:8000
```

The API reads rollup tables only, never `messages`: a request never aggregates
and never touches the raw JSON. That means **rollups must be built before the
site has anything to show**, and `rollups status` is how you check.

### API

| Endpoint | Who | Returns |
|---|---|---|
| `GET /api/health` | anyone | liveness plus the banner state (`null`, `stale`, `token_dead`) |
| `GET /api/bootstrap` | viewer | the whole dashboard payload in one request |
| `GET /api/words?q=` | viewer | search across the full word index |
| `GET /api/admin/status` | admin | rollup counts, staleness, worker health, job counts |
| `GET /api/admin/config` · `PUT` | admin | operator overrides (timezone, ignore list, allowlist policy) |
| `GET|POST /api/admin/jobs` | admin | the job queue |
| `POST /api/admin/recalculate` | admin | queue a full rollup rebuild |
| `GET /api/admin/search?q=` | admin | message text — the one endpoint that returns it |

Login is Discord OAuth, scope `identify` only. A viewer is the admin id, an
explicitly allowed id, or somebody who has posted; an admin is `ADMIN_DISCORD_ID`.
Sessions are rows, stored hashed, so they are revocable.

### How the aggregates are laid out

Fourteen rollup families, each keyed by `channel_id` first and by a grain rather
than by a panel, so one table feeds many charts. Every rebuild is full and
inside one transaction, and `rollup_meta` records what it was built from
(`computed_at`, `source_count`, `source_max_message_id`, `timezone`,
`config_hash`) — which is what makes "is this stale?" a query rather than a
guess. Changing the timezone, merging identities, or editing the stopword list
invalidates every aggregate, so `rollups status` will say `stale` until you
rebuild.
