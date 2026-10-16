# discord-scraper

Scrape a Discord channel into a local SQLite database, analyse it from the CLI,
and optionally serve a stats site over it.

**Install one of three ways, depending on how much you want:**

| Tier | Install | What you get |
|---|---|---|
| 1 — CLI only | `pipx install discord-scraper` | scrape a channel; the text analysis CLI. No dependencies beyond `httpx` |
| 2 — CLI + site | `pipx install "discord-scraper[web]"` then `discord-scraper serve` | the full stats site on localhost |
| 3 — Container | `docker run -v ./data:/data --env-file .env discord-scraper` | the same site, with neither Python nor Node installed on the host |

The CLI is tier 1 and stays a first-class interface, not a leftover: it is how
the tool is useful with zero infrastructure. The web is additive.

> **ToS caveat, read it:** scraping uses a **user-account token** against
> Discord's private REST API, which violates Discord's Terms of Service
> ("self-botting") and can get the account actioned. Use a dedicated alt
> account, throttle conservatively, and accept that risk knowingly. The
> compliant alternative is Discord's own data export
> (Settings → Privacy & Safety → Request Data), which this tool can also import.

**Jump to:** [quickstart](#quickstart) · [the stats site](#the-stats-site) ·
[operations](OPERATIONS.md) · [schema](#schema) · [analysis CLI](#analysis)

## Quickstart

```bash
git clone <this repo> && cd discord-scraper
python3 -m venv .venv && . .venv/bin/activate

# 1. secrets in .env, settings in config.toml — never the other way round
cp .env.example .env            # then put DISCORD_TOKEN in it
cp config.example.toml config.toml   # then set source.channel_id

# 2. tier 1: scrape and analyse, stdlib only
python3 main.py                 # or: discord-scraper scrape
python3 -m analysis stats       # or: discord-scraper analyze stats

# 3. tier 2: the site
pip install -e ".[web]"
discord-scraper rollups rebuild     # the API reads rollups, never `messages`
discord-scraper serve               # http://127.0.0.1:8000
```

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

## The stats site

The site is a second, optional layer over the same database: precomputed rollup
tables plus a read-only, authenticated API and a React frontend that the API
serves from the same port.

```bash
discord-scraper rollups rebuild     # required before the site has anything to show
discord-scraper rollups status      # row counts + current/stale per family
discord-scraper serve               # API + ingest worker + the built SPA, :8000
```

**The API never aggregates on request.** It reads fourteen rollup tables and
reports when they were built; a request never scans `messages` and never touches
the 483 MB of raw JSON. That is why the rebuild is a separate step, and why
`rollups status` matters: after a timezone change, an identity merge or a
stopword edit, the tables are **stale** until you rebuild.

### Routes

| Route | Who | What |
|---|---|---|
| `/` | signed-in viewer | the dashboard: 36 panels in six sections |
| `/login` | anyone | Discord OAuth, scope `identify` only |
| `/admin` | admin | data source, who shows, identity merge, ingest, rollup cache, access |
| `/api/health` | anyone | liveness plus the banner state |
| `/api/bootstrap` | viewer | the whole dashboard payload in one request |
| `/api/recent` | viewer | the last N messages — see the privacy note below |
| `/api/words` | viewer | search the full word index beyond the shipped 500 |
| `/api/admin/*` | admin | status, config, jobs, recalculate, upload, search |

Two endpoints deliberately break the "aggregates only" rule, and both are
viewer-gated: `/api/recent` (truncated to ~120 characters, no attachments,
embeds or raw JSON; `ui.show_recent = 0` removes it entirely) and the admin
search. Everything else returns numbers.

### Access

- Secrets, environment only: `DISCORD_TOKEN`, `DISCORD_CLIENT_SECRET`,
  `SESSION_SECRET`, `ADMIN_DISCORD_ID`.
- Settings, `config.toml` only: channel, timezone, counted message types, bot
  rule, stopword packs, identity merges, OAuth client id and redirect URI.
- A viewer is the admin id, anyone with an explicit `allow` row, or anybody who
  has posted. An admin is `ADMIN_DISCORD_ID`. Sessions are rows in SQLite,
  stored hashed, so they are revocable.
- For a quick look on a phone there is `discord-scraper serve --dev-login`,
  which enables `/auth/dev-login` and signs anyone who can reach the port in as
  viewer or admin. **Development only** — it prints a warning, and it must never
  be used on a public host.

### Deploying

`Dockerfile` is a two-stage build (Node builds the SPA, Python runs the
server) and `docker-compose.yml` runs the API and the worker as separate
services over one shared volume, so the Discord token can live on the machine
that serves no HTTP. Backup, restore, the serve shapes and the tunnel are in
[OPERATIONS.md](OPERATIONS.md).

## Development

```bash
# backend
python3 -m pytest -q

# frontend (Vite dev server proxies /api to :8000)
cd web && pnpm install && pnpm dev
cd web && pnpm test && pnpm typecheck
```

The design spec and the implementation plans live in `docs/superpowers/`, which
is **gitignored** — they describe one specific chat and are not part of the
shipped tool.

## Credits

English stopwords come from [stopwords-iso](https://github.com/stopwords-iso/stopwords-iso)
(MIT, © 2020 Gene Diaz); the licence ships beside the data in
`src/discord_scraper/data/LICENSE-stopwords.txt`, along with an opt-in Malay
pack. Charting is [ECharts](https://echarts.apache.org/) (Apache-2.0) and its
word-cloud extension. UI chrome follows [shadcn/ui](https://ui.shadcn.com/)
conventions over Radix primitives.
