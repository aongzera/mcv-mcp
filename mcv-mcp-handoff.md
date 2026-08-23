# Handoff — MyCourseVille MCP Server

**Date:** 2026-08-17
**Repo:** `C:\Users\velys\Documents\mcp_mcv`
**Status:** Feature-complete and verified against the live site. 38 tests pass.

---

## What this project is

An MCP server giving an AI agent access to the user's [MyCourseVille](https://www.mycourseville.com)
account (Chulalongkorn University's LMS). Four original requirements, all met:

1. Detect newly uploaded assignments
2. Get each assignment's deadline **and** description
3. Pull/download course materials
4. Detect when a grade is released

Architecture, tool list, config vars and known limits are documented in **`README.md`** —
read that first, don't re-derive it.

## Do not re-discover these

**`docs/ENDPOINTS.md`** is the hard-won reverse-engineering record. It contains the full
endpoint table, login flow, every CSS selector, and the gotchas. Read it before touching
`client.py` or `parsers.py`. Highlights it already covers:

- The `X-Requested-With` bootstrap trap (costs an hour to rediscover)
- MCV's tabs are **URL path segments**, not an AJAX `ocv_mode` parameter
- All MCV times are **Bangkok (UTC+7)** and unlabelled
- Visible date cells are unparseable div stacks; the `.sr-only` copy is the source of truth
- `"Not ready"` = unreleased score; those rows are stored deliberately

## State of the work

Last verified full sync: **9 courses, 20 assignments** (all with parsed deadlines),
**112 materials, 69 graded items** (2 released). A second sync reported 0 added / 0 changed,
so change detection is quiet until something actually moves. `download_material` was
confirmed on a 2.8 MB PDF including text extraction. The MCP stdio handshake lists all
10 tools.

Working tree is clean of debugging cruft. Nothing has been committed.

---

## Where we stopped — two open threads

### Thread A (immediate): should the project go into git?

The user asked *"should I put the mcp code onto git first would that make it easier"*.
Answer is **yes** — MCV's markup is the fragile part, and `git pull` beats re-`scp`-ing a
tarball every time a selector breaks. But it was **not done**, because a check surfaced a
trap that must be handled first:

| Finding | Implication |
|---|---|
| `git rev-parse --show-toplevel` → `C:/Users/velys/Documents` | The git root is the user's **entire personal Documents folder** (coursework, Obsidian vault, game dirs, `node_modules`). **Never push this repo.** |
| `mcp_mcv` is **not tracked** by that outer repo | Nothing to untangle — a clean `git init` inside `mcp_mcv` works. |
| `.gitignore` line 1 is `.env`, verified via `git check-ignore -v` | Protection is in place. |
| `.env` currently holds a **real Chula SSO password** | Verify `git status` shows no `.env` **before** the first commit. A leaked SSO password exposes far more than the LMS. |

**Next action:** `git init` inside `mcp_mcv`, confirm `.env` and `.venv/` are absent from
`git status`, make the initial commit, then push to a **private** remote. Recommend private
regardless — it's personal course tooling.

### Thread B: deploy to the user's Hermes agent

The user runs **Hermes Agent** (Nous Research) on a Hostinger box and wants this server
attached to it. Research is done; the plan below was delivered but **not executed** — the
user has run none of these steps yet.

Environment facts already established (don't re-probe):

- Ubuntu x86_64, **Python 3.13.5**, `uv` at `/usr/local/bin/uv`, git present — all prereqs met
- It is a **Docker container** (`/.dockerenv` present)
- **`/opt/data` is a persistent bind mount**; `/opt/hermes` is container filesystem and is
  wiped on image update → the server must live under `/opt/data`
- Hermes home is `/opt/data`, so its config is **`/opt/data/config.yaml`** (docs say
  `~/.hermes/config.yaml` — same file, relocated)
- `config.yaml` has **no `mcp_servers:` key yet**
- Agent runs as user **`hermes`**; the user's shell is `root`
- Hermes supports MCP over stdio **and** HTTP. **stdio is the right choice** — colocated,
  nothing exposed, no auth/TLS needed.

Deployment steps as given:

1. `scp` the prebuilt tarball (see Artifacts) to the box, extract to `/opt/data/mcp_mcv`, `uv sync`
2. Write `.env` there with `MCV_STATE_DIR=/opt/data/mcp_mcv/state` (the default
   `~/.local/share` is container filesystem and gets wiped) and `MCV_POLL_ENABLED=0`
3. `chown -R hermes:hermes /opt/data/mcp_mcv` and `chmod 600 .env`
4. Run the selftest **as the `hermes` user**, not root, or the cookie jar and SQLite file
   end up root-owned and unwritable by the agent
5. Add to `/opt/data/config.yaml`:

```yaml
mcp_servers:
  mcv:
    command: "/usr/local/bin/uv"          # absolute - gateway PATH isn't guaranteed
    args: ["--directory", "/opt/data/mcp_mcv", "run", "mcv-mcp"]
    enabled: true
    timeout: 120
    connect_timeout: 60
```

6. `/reload-mcp`, then `hermes mcp test mcv`

**Deliberate omission:** no `env:` block, so no credentials land in the agent's own config
(the agent can read and dump that file). `load_dotenv()` walks up from the module path and
finds `/opt/data/mcp_mcv/.env` regardless of working directory — this was verified.

**Open design point:** polling is disabled above on purpose. Hermes tears down idle MCP
servers (`idle_timeout_seconds`, `max_lifetime_seconds` exist in its schema), so the
in-process poller thread would die silently and new-assignment detection would stop
working without any error. Recommended replacement is Hermes' own **`hermes cron`** — have
the *agent* periodically call `sync_now` then `whats_new` and notify the user over its
gateway (Telegram/Discord/etc.). The user was asked to run `hermes cron --help`; **that
output has not come back yet.** Writing that schedule entry is the last unfinished piece.

---

## Artifacts

Session scratchpad — `%TEMP%\claude\C--Users-velys-Documents-mcp-mcv\a667ec50-…\scratchpad\`
(session-specific; may be cleaned):

- **`mcv-mcp-deploy.tar.gz`** — 76 KB deployable archive, `.env`/`.venv`/`.git` excluded,
  `uv.lock` included. Rebuild with the `tar` invocation from the transcript if missing.
- `probe_*.py`, `scan_courses.py`, `dump_blocks.py`, `check*.py` — live-session probes used
  to reverse-engineer the endpoints. Reusable when MCV changes; they log in via `.env`.
- `tab_*.html`, `worksheet.html`, `course.html`, `home.html` — real captured page HTML.
  These are the fixture source. Re-capturable via the recipe in `docs/ENDPOINTS.md`.

## Constraints that stay in force

- Credentials live **only** in the gitignored `.env`. Never commit, never log, never echo.
- Never print cookie values or OAuth `?code=` params.
- State (SQLite, cookies, downloads) stays **outside** the repo.
- Polling stays polite: default 30 min, floor 10 min, serialized with a 0.4 s delay. This is
  a university server and one student's personal tool.
- **Do not bulk-enumerate MCV AJAX command names.** An earlier attempt was blocked by the
  permission classifier and that was accepted, not worked around. It hammers a university
  server and looks like an attack. Read names from the page's own JS or DevTools instead.

## Suggested skills

Call these via the **Skill** tool:

- **`security-review`** — run before the first `git push` and before credentials go onto the
  VPS. This is the highest-value one: a real SSO password, a `.gitignore` that must hold,
  and a deployment that moves that password to a rented box.
- **`code-review`** — if `parsers.py` or `client.py` change. Parser edits are exactly where
  a silent zero-rows regression hides.
- **`init`** — worth running once inside `mcp_mcv` after `git init`, to generate a
  `CLAUDE.md`. Future sessions currently have to re-read `README.md` + `docs/ENDPOINTS.md`
  from scratch.

Not needed: `dataviz`, `artifact-*` (no visual deliverable), `claude-api` (no Anthropic SDK
in this project).

## Notes on working with this user

- They are a Chula computer-engineering student; comfortable with the shell but new to MCP
  deployment specifics. Direct, concrete commands land better than prose.
- Verify claims against the live site rather than reasoning from assumptions — three of the
  four bugs fixed this session were wrong guesses about MCV's markup that only a real
  request exposed.
