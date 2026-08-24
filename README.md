# MyCourseVille MCP Server

An MCP server that gives an AI agent access to your [MyCourseVille](https://www.mycourseville.com)
account: new assignments with their deadlines and descriptions, course announcements,
course materials, and released grades — plus a "what changed since last time" feed.

Personal tool for your own account data. Keep the poll interval polite and never commit
your credentials.

## How it works

MCV has an official OAuth2 API (`/api/v1/public/get/*`), but it requires registering an
app to get a `client_id`/`client_secret`, and its endpoint names aren't publicly
documented. This server instead logs in exactly like the MCV web client does — through the
IT Chula SSO button by default, or the plain MCV form — then reads the same server-rendered
course pages the site serves to a browser: the course home for announcements and
materials, `/assignment` for deadlines, and `/portfolio-<student id>` for scores. See
[docs/ENDPOINTS.md](docs/ENDPOINTS.md).

Fetched data lands in a local SQLite snapshot. Every sync diffs against that snapshot and
appends to an `events` log — that log is what `whats_new` reads, so the server can tell you
what appeared since you last looked. A background poller refreshes on an interval so a new
assignment is noticed even when no chat is open.

**On "new":** MCV doesn't reliably expose a posted-at timestamp, so `first_seen` — when
this server first observed an item — is the proxy. The first sync backfills everything
quietly rather than reporting your entire semester as new.

## Setup

```bash
uv sync --extra dev
cp .env.example .env      # then fill in MCV_USERNAME / MCV_PASSWORD
uv run mcv-mcp --selftest
```

`--selftest` logs in, syncs once, and prints how many courses, assignments, materials and
grades it found. Get that working before wiring up the agent.

Register with Claude Code:

```bash
claude mcp add mcv -- uv --directory C:\Users\velys\Documents\mcp_mcv run mcv-mcp
```

Then ask: *"What's due this week?"*, *"Anything new on MCV since yesterday?"*,
*"Download the latest slides for Comp Prog."*

## Tools

| Tool | What it answers |
|---|---|
| `whats_new` | New/changed assignments, announcements, materials and grades since a point in time |
| `list_upcoming_deadlines` | What's due in the next N days, soonest first |
| `list_courses` | Enrolled courses, optionally one semester |
| `list_assignments` | Assignments with deadline + description |
| `get_assignment` | Full detail for one assignment |
| `list_announcements` | Instructor announcements, newest first, with their full text |
| `get_announcement` | Full detail for one announcement |
| `list_materials` | Files posted in a course (folder, name, URL) |
| `download_material` | Save a file locally; extracts PDF text so it can be read |
| `list_grades` | Released scores |
| `sync_now` | Force a refresh (`full=true` for all semesters) |
| `auth_status` | Diagnostics: login state, last sync, row counts |

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `MCV_USERNAME` / `MCV_PASSWORD` | — | Your MCV credentials |
| `MCV_LOGIN_METHOD` | `itchula` | `itchula` for the IT Chula SSO button, `mcv` for the plain form |
| `MCV_POLL_MINUTES` | `30` | Poller interval (floor 10) |
| `MCV_POLL_ENABLED` | `1` | Set `0` to disable background polling |
| `MCV_STATE_DIR` | `%LOCALAPPDATA%\mcv_mcp` | Where the DB, cookies and downloads live |

State is kept **outside the repo**. Cookies are persisted, so restarts don't re-login.

Run the poller separately (e.g. under Task Scheduler) with:

```bash
uv run python -m mcv_mcp.poller
```

## Layout

```
src/mcv_mcp/
  config.py    settings, URLs, state paths
  client.py    Chula SSO login, cookie persistence, throttled AJAX transport
  parsers.py   HTML -> dicts (pure functions, fixture-tested)
  store.py     SQLite snapshot + event log
  sync.py      orchestration + background poller
  server.py    MCP tool definitions
```

MCV's markup is the fragile part, and it's confined to `parsers.py`. If something stops
returning rows, capture a fresh fixture (see docs/ENDPOINTS.md) and fix the selector there.

## Status

Working against a real account: login, all four parsers, downloads and change detection are
verified end to end. A representative sync pulled 9 courses, 20 assignments (every one with
a parsed deadline), 112 materials and 69 graded items; the following sync reported zero
changes, so the diff log stays quiet until something actually moves.

Known limits:

- **Grade release is detected by polling, not pushed.** A score appearing shows up as a
  `grade_changed` event within one poll interval (default 30 min).
- **Only the current semester** is synced unless you pass `full=true` to `sync_now`.
- **MCV's markup is the fragile part.** If a parser starts returning zero rows, capture a
  fixture and fix the selector — see [docs/ENDPOINTS.md](docs/ENDPOINTS.md).

## Development

```bash
uv run pytest
```
