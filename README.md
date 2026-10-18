# discord-message-scraper

Pull a Discord channel's full history into a local SQLite database, roll it up
into queryable aggregates, and analyse it. **Zero dependencies** — Python 3.11+
and the standard library, nothing else.

The dashboard that renders these rollups lives in
[discord-message-stats](https://github.com/BobbyNooby/discord-message-stats),
which consumes this repo as a library. Our own themed instance with the Wrapped
recap is private.

> **ToS caveat, read it:** scraping uses a **user-account token** against
> Discord's private REST API, which violates Discord's Terms of Service
> ("self-botting") and can get the account actioned. Use a dedicated alt
> account, throttle conservatively, and accept that risk knowingly. The
> compliant alternative is Discord's own data export
> (Settings → Privacy & Safety → Request Data), which this tool can also import.

**Jump to:** [quickstart](#quickstart) · [which token](#which-token) ·
[commands](#commands) · [schema](#schema) · [analysis](#analysis)

## Quickstart

```bash
git clone https://github.com/BobbyNooby/discord-message-scraper
cd discord-message-scraper

# secrets in .env, settings in config.toml — never the other way round
cp .env.example .env                 # then put DISCORD_TOKEN in it
cp config.example.toml config.toml   # then set source.channel_id

python3 main.py scrape               # needs the real token; resumes if interrupted
python3 main.py rollups rebuild --db output/messages.db   # the aggregates the dashboard reads
python3 analysis.py                  # a text report of what you scraped
```

Nothing needs installing for those: `main.py` and `analysis.py` put `src/` on
the path. `pip install -e .` adds the `discord-scraper` console script, which
carries exactly the same subcommands (`discord-scraper scrape`, …).

Only the scrape needs `DISCORD_TOKEN`; the rollups and the analysis read the
local database. An empty database is not a crash: the analysis prints
`No database at … — run discord-scraper scrape first` and exits 0.

## Which token

Both are sent the same way — in the `Authorization` header — but they
authenticate **different principals**, and that difference is the whole story:

- A **bot token** (from the [Developer Portal](https://discord.com/developers/applications))
  authenticates a separate bot user. It is the sanctioned path, it is what the
  documented Bot API expects, and it cannot get your account actioned. The
  catch: a bot must be **invited** to the server and granted read access, and
  **a bot cannot join a group DM at all** — Discord does not support that.
- A **user token** authenticates *you*. It is the only way to read a group DM,
  and it is also **self-botting**: automating a user account breaks Discord's
  Terms of Service and can get the account actioned. If you go this way, use a
  dedicated alt account, not your own.

This tool sends whatever you give it verbatim, so both work. Choose with your
eyes open; the legal risk is yours, not the tool's.

## Commands

| Command | Purpose |
|---|---|
| `python3 main.py scrape` | fetch history (resumable) |
| `python3 main.py rollups rebuild --db output/messages.db` | rebuild every rollup family (one writer at a time) |
| `python3 main.py rollups status --db output/messages.db` | what is stale and why |
| `python3 main.py analyze <cmd>` | stats, top-authors, top-words, emoji, reactions, search… |
| `python3 main.py serve --worker` | run the ingest worker that the dashboard's queue drives |

`serve` is worker-only on purpose: the HTTP dashboard, OAuth, the SPA and the
container all live in
[discord-message-stats](https://github.com/BobbyNooby/discord-message-stats).
`rollups` reads a database you name with `--db`, so it works against a copy.

Settings live in `config.toml`; **secrets live only in `.env`**. Operators can
change the channel, timezone or stopwords at runtime through the dashboard,
which stores an override beside the database — `--ignore-overrides` on any
command ignores it:

```sh
python3 main.py scrape --config /path/to/config.toml
python3 main.py scrape --ignore-overrides
```

## Layout

```
main.py               `python3 main.py scrape` — puts src/ on the path, no install
analysis.py           `python3 analysis.py` — shim for the analysis CLI, no install
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
  worker/             the scheduled ingest worker
  rollups/            the precomputed rollup tables the dashboard reads
  data/               packaged stopword packs (+ their licence)
output/
  messages.db         the database (created on first run)
sql/
  analysis.sql        starter queries for analysis
tests/                the pytest suite
```

## Setup

```sh
cp .env.example .env                 # secrets: just the token
cp config.example.toml config.toml   # everything else
```

`.env` holds secrets only, and the one it needs is:

- `DISCORD_TOKEN` — see [Which token](#which-token)

`config.toml` holds every setting. The ones a first run needs are:

- `source.channel_id` — the DM/group channel ID (visible in the browser client
  URL: `/channels/@me/<id>`)
- `source.batch_size` — messages per request (1–100; default 50)

The rest — timezone, counted message types, the bot rule, stopword packs,
identity merges, OAuth client id and redirect URI — have neutral defaults and
are documented inline in `config.example.toml`.

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
python3 -m pip install -e ".[dev]"
python3 -m pytest -q
```

No network: the catch-up tests run against a temp SQLite file with Discord's
REST calls faked by `httpx.MockTransport` (which is why `httpx` is a dev
dependency of an otherwise dependency-free tool), and the rollup tests run
against an in-memory database with the real schema. The resume paths get their
own tests — a pass killed mid-update, a re-run that is already current, a
`--max-batches` stop, and new messages arriving while a hole is still open.

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

Once installed (`pip install -e .`) the analysis commands are subcommands of
`discord-scraper`:

```sh
discord-scraper analyze                                                # default report: charts + text dump
discord-scraper analyze stats
discord-scraper analyze report --bucket auto # auto bucket from the data span
discord-scraper analyze activity --by month # or day / hour
discord-scraper analyze top-authors --limit 10
discord-scraper analyze reactions           # emoji leaderboard (raw JSON)
discord-scraper analyze attachments         # file-type breakdown (raw JSON)
discord-scraper analyze mentions            # most-@'d people (raw JSON)
discord-scraper analyze most-replied
discord-scraper analyze search "some text"
discord-scraper analyze top-words --author exampleuser  # word frequency
discord-scraper analyze emoji               # emoji used in message text
```

From a bare checkout the same commands run as a module with `src/` on the path
(`PYTHONPATH=src python3 -m discord_scraper.analysis stats`), or through the
checkout shim, which forwards its arguments:

```sh
python3 analysis.py stats
python3 analysis.py activity --by month
python3 analysis.py            # no command = the default report
```

`discord-scraper analyze` (or `discord-scraper analyze report`) with no other
arguments generates `output/report/` in both **words** and **messages** modes:

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

## The dashboard

The site is a separate repo: precomputed rollup tables plus a read-only,
authenticated API and a React frontend served from one port. It pins this tool
as a submodule under `vendor/`.

```bash
discord-scraper rollups rebuild --db output/messages.db  # required before the site has anything to show
discord-scraper rollups status --db output/messages.db   # row counts + current/stale per family
```

**The API never aggregates on request.** It reads the rollup tables this tool
builds and reports when they were built; a request never scans `messages`. That
is why the rebuild is a separate step, and why `rollups status` matters: after
a timezone change, an identity merge or a stopword edit, the tables are
**stale** until you rebuild. See the
[dashboard's README](https://github.com/BobbyNooby/discord-message-stats#readme)
for the site, the OAuth login, the admin board and the container.

## Privacy

The rollups are aggregates. Message text never leaves the database, and the
stopword packs in `data/` exist so the word index stays meaningfully "what
people say", not "what the scraper happened to see".

## Credits

English stopwords come from [stopwords-iso](https://github.com/stopwords-iso/stopwords-iso)
(MIT, © 2020 Gene Diaz); the licence ships beside the data in
`src/discord_scraper/data/LICENSE-stopwords.txt`, along with an opt-in Malay
pack.
