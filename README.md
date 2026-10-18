# discord-scraper

Scrape a Discord channel into a local SQLite database, analyse it from the CLI,
and optionally serve a stats site over it.

**Install one of three ways, depending on how much you want:**

| Tier | Install | What you get |
|---|---|---|
| 1 — CLI only | `pipx install discord-scraper` | scrape a channel; the text analysis CLI. No dependencies at all — HTTP is stdlib `urllib` |
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
cp .env.example .env                 # then put DISCORD_TOKEN in it
cp config.example.toml config.toml   # then set source.channel_id

# 2. tier 1: scrape, then analyse. Nothing to install for either.
python3 main.py scrape               # needs the real token; resumes if interrupted
python3 analysis.py                  # default report: charts + text dump

# 3. tier 2: the site
pip install -e ".[web]"
discord-scraper rollups rebuild --db output/messages.db
discord-scraper serve                # http://127.0.0.1:8000
```

Only the scrape needs `DISCORD_TOKEN`; the analysis and the site read the local
database. A scrape with no messages in it yet leaves the analysis with nothing
to report, so run the scrape to completion first. The named analyses
(`stats`, `top-authors`, …) are in [Analysis](#analysis).

## Layout

```
main.py               `python3 main.py scrape` — puts src/ on the path, no install
analysis.py           `python3 analysis.py` — the default report, same trick
config.example.toml   copy to config.toml (settings; gitignored)
.env.example          copy to .env (secrets only; gitignored)
stopwords.d/          your own *.txt word lists, if any (optional)
src/discord_scraper/
  cli.py              the `discord-scraper` console script: scrape/analyze/rollups/serve
  config.py           secrets from .env, settings from config.toml
  overrides.py        operator overrides stored beside the database
  pipeline.py         the scrape itself (catch-up + backfill, resume points)
  scraper/            Discord REST client, schema, transform, constants
  analysis/           read-only queries, text report and matplotlib charts
  api/                the FastAPI app, auth, and the SPA it serves
  worker/             the scheduled ingest worker
  rollups/            the precomputed rollup tables the API reads
  data/               packaged stopword packs (+ their licence)
output/
  messages.db         the database (created on first run)
sql/
  analysis.sql        starter queries for analysis
web/                  the React frontend (pnpm)
tests/                the pytest suite
```

## Setup

```sh
cp .env.example .env                 # secrets: just the token
cp config.example.toml config.toml   # everything else
```

`.env` holds secrets only, and the one it needs is:

- `DISCORD_TOKEN` — a **user** token (bots can't read DMs they weren't added to)

`config.toml` holds every setting. The ones a first run needs are:

- `source.channel_id` — the DM/group channel ID (visible in the browser client
  URL: `/channels/@me/<id>`)
- `source.batch_size` — messages per request (1–100; default 50)

The rest — timezone, counted message types, the bot rule, stopword packs,
identity merges, OAuth client id and redirect URI — have neutral defaults and
are documented inline in `config.example.toml`.

The channel can be changed without editing the file: saving it in the admin
board (`/admin`) stores an override next to the database, and the scrape, the
worker and `rollups rebuild` all resolve through that override. The CLI reads a
different `config.toml` with `--config`, and ignores stored overrides entirely
with `--ignore-overrides`:

```sh
python3 main.py scrape --config /path/to/config.toml
python3 main.py scrape --ignore-overrides
```

## Run

```sh
python3 main.py scrape                 # full scrape
python3 main.py scrape --max-batches 3 # quick test run (3 batches of 50)
python3 main.py scrape --batch-size 25 # gentler on the API (1-100; default 50)
```

`scrape` is a subcommand: bare `python3 main.py` prints the available commands.
Once installed, the same flags work on `discord-scraper scrape`.

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
- 50 messages per request by default — tune with `--batch-size` or
  `source.batch_size` in `config.toml` (the flag wins; 1–100, and 100 is
  Discord's hard maximum, so the default 50 keeps you conservative), ~0.8s
  between requests; honors `retry_after` on HTTP 429

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
python3 -m pip install pytest   # or: pip install -e ".[dev]"
python3 -m pytest -q
```

No network: the catch-up tests run against a temp SQLite file with only the
HTTP call faked, and the rollup tests run against an in-memory database with
the real schema. The resume paths get their own tests — a pass killed
mid-update, a re-run that is already current, a `--max-batches` stop, and new
messages arriving while a hole is still open.

`python3 -m unittest discover -s tests -t .` also works, but it only finds the
few `unittest.TestCase` classes, not the pytest-style functions.

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

From the checkout these run as a module, with `src/` on the path; once the
package is installed (`pip install -e .`) drop the `PYTHONPATH=src` prefix.

```sh
python3 analysis.py                                                    # default report: charts + text dump
PYTHONPATH=src python3 -m discord_scraper.analysis stats
PYTHONPATH=src python3 -m discord_scraper.analysis report --bucket auto # auto bucket from the data span
PYTHONPATH=src python3 -m discord_scraper.analysis activity --by month # or day / hour
PYTHONPATH=src python3 -m discord_scraper.analysis top-authors --limit 10
PYTHONPATH=src python3 -m discord_scraper.analysis reactions           # emoji leaderboard (raw JSON)
PYTHONPATH=src python3 -m discord_scraper.analysis attachments         # file-type breakdown (raw JSON)
PYTHONPATH=src python3 -m discord_scraper.analysis mentions            # most-@'d people (raw JSON)
PYTHONPATH=src python3 -m discord_scraper.analysis most-replied
PYTHONPATH=src python3 -m discord_scraper.analysis search "some text"
PYTHONPATH=src python3 -m discord_scraper.analysis top-words --author exampleuser  # word frequency
PYTHONPATH=src python3 -m discord_scraper.analysis emoji               # emoji used in message text
```

`analysis.py` (and the `report` command) with no command generates
`output/report/` in both **words** and **messages** modes:

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

`MessageDB` wraps the DB so you write plain Python, not SQL + `json.loads`
(this needs the package installed: `pip install -e .`):

```python
from discord_scraper.analysis import MessageDB

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
discord-scraper rollups rebuild --db output/messages.db  # required before the site has anything to show
discord-scraper rollups status --db output/messages.db   # row counts + current/stale per family
discord-scraper serve                                    # API + ingest worker + the built SPA, :8000
```

`rollups` reads a database you name with `--db`, so it works against a copy
without touching the live one. `serve` defaults to `output/messages.db`.

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
- Settings, `config.toml`: channel, timezone, counted message types, bot rule,
  stopword packs, identity merges, OAuth client id and redirect URI. The admin
  board can override the channel, the timezone, extra stopwords, the allowlist
  policy and the schedule; `--ignore-overrides` bypasses those stored values.
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
