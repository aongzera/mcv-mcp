# MyCourseVille endpoints

What this server talks to, and how to re-discover it when MCV changes.

## The official OAuth2 API (not used)

MCV runs a real OAuth2 server. Probed live:

| Endpoint | Unauthenticated response |
|---|---|
| `GET /api/oauth/authorize` | `401 {"error":"invalid_client"}` without a registered app |
| `POST /api/oauth/access_token` | `400 … Check the "grant_type" parameter` |
| `GET /api/v1/public/get/*` | `400 … Check the "access token" parameter` |

This is the sanctioned route, but it needs a `client_id`/`client_secret` from registering
an app while logged in, and its resource paths aren't publicly documented. Auth middleware
runs *before* routing, so `/api/v1/public/get/<anything>` returns an identical error
whether or not the path exists — you cannot map it without a token. If you ever obtain
client credentials, this is the better foundation and worth migrating `client.py` onto.

## Login (what we actually use)

MCV's own frontend links to three login entry points, all through its first-party OAuth
client `mycourseville.com`:

```
/api/oauth/authorize?response_type=code&client_id=mycourseville.com&redirect_uri=https://www.mycourseville.com[&login_page=itchula|google]
```

- no `login_page` → redirects to `/api/login` (MCV account)
- `login_page=itchula` → redirects to `/api/chulalogin` (Chula SSO) ← **default here**

**The critical gotcha:** send `X-Requested-With: XMLHttpRequest` on the bootstrap and MCV
answers a bare `401 Unauthorized.` instead of redirecting to the login form. The header
belongs on the AJAX endpoints only, never on navigation. `client.py` sets it per-request
for exactly that reason — this cost an hour to find, don't undo it.

Flow:

1. `GET` the bootstrap URL above (no XHR header) → lands on the login form, sets
   `laravel_session` + `XSRF-TOKEN`
2. Scrape `<input type="hidden" name="_token" value="…">` from that page
3. `POST` the form:

   | Login page | Action | Fields |
   |---|---|---|
   | Chula SSO | `/api/chulalogin` | `_token`, `username`, `password`, `remember` |
   | MCV account | `/api/login` | `_token`, `loginfield=name`, `name`, `password`, `remember` |

Note the MCV form uses `name`, not `username`.

**A 200 does not mean you are logged in** — a rejected login also renders 200. Verify by
calling `cvhomepanel_get_filter` and checking that non-`CHULAMOOC` courses come back;
that's what `MCVClient._probe_session` does.

## Internal AJAX endpoints

`POST https://www.mycourseville.com/?q=courseville/ajax/<command>` with the session cookie
and `X-Requested-With: XMLHttpRequest`.

| Command | Payload | Returns | Status |
|---|---|---|---|
| `cvhomepanel_get_filter` | `yearsem`, `role=all`, `type=course` | JSON `{status, data:[{cv_cid, course_no, title, …}]}` | **confirmed** |
| `getactivepanelcontent` | — | JSON `{html}` — active assignments, relative "dues in …" wording | **confirmed** |

## Course pages (plain GETs — this is where the content is)

MCV's course tabs are **URL path segments, not an AJAX parameter**. Every one is
server-rendered, so an authenticated `GET` returns the full markup:

| URL | Contains | Parsed by |
|---|---|---|
| `/?q=courseville` | `<select id="all-yearsem-select">` — the semester codes | `client.get_semesters` |
| `/?q=courseville/course/<cv_cid>` | course home: **materials**, with direct S3 file links | `parse_materials` |
| `/?q=courseville/course/<cv_cid>/assignment` | **assignment list** with out/due dates | `parse_assignments` |
| `/?q=courseville/course/<cv_cid>/portfolio-<student id>` | **graded items and scores** | `parse_grades` |
| `/?q=courseville/worksheet/<cv_cid>/<item id>` | one assignment's **instruction text** | `parse_worksheet_description` |

Other tabs exist and are unused: `map`, `media_gallery`, `playlist`, `wlrlist`, `schedule`,
`discussion`, `meeting`, `group`, `about`.

### Selectors that matter

| What | Selector |
|---|---|
| Semester dropdown | `select#all-yearsem-select`; the **current** semester is its `data-value`, *not* the first `<option>` (future semesters are listed above it) |
| Material folder | `.cv-course-home-folder-container [data-part="title"]` — absent in folder-less courses |
| Material row | `table.cv-course-home-material-table tr`; name from `td[data-col=title] a`, file URL from `td[data-col=action] a`, stable id from `[data-nid]` |
| Assignment row | `#cv-assignment-table tr`, link matching `/worksheet/<cid>/<item id>` |
| Assignment due date | `td.cv-due-col .sr-only` → `"Due on 23 August 2026 at 23:59"` |
| Grade row | `#courseville-portfolio-gradeditem-table tr[content_id]`; skip `.courseville-point-table-total-row` |
| Grade max score | third cell's `.sr-only` → `"from 30"` |
| Assignment description | `#courseville-worksheet-instruction-body` |

**Dates:** never scrape the visible date cell — it is a stack of styled divs (`Aug`, `23`,
`2026`) that concatenates into garbage. The `.sr-only` copy next to it is unambiguous
prose. All MCV times are **Bangkok (UTC+7)** and unlabelled; `parsers.BANGKOK` handles the
conversion to UTC.

**Unreleased scores** read `Not ready`. Those rows are stored anyway, so the release shows
up as a `grade_changed` event on the next sync — that is the "my grade was uploaded"
signal. `parsers.is_released()` is the test.

### Probing for a command's existence

An unknown command returns `{"status":0,"msg":"Invalid command (<name>)"}`; a real one that
needs auth returns a bare `{"status":0}`. That distinction is a reliable existence check —
but **don't bulk-scan names.** It hammers a university server and looks exactly like an
attack. Read the names out of the page's own JS instead, or watch DevTools.

## Capturing a fresh fixture

When MCV changes its markup, a parser starts returning zero rows. Capture the page and fix
the selector against it:

```python
from mcv_mcp.client import MCVClient
from mcv_mcp.config import load_config

with MCVClient(load_config()) as c:
    c.login()
    html = c.get_course_page("<cv_cid>", "assignment")   # or None / f"portfolio-{username}"
    open("course_assignment.html", "w", encoding="utf-8").write(html)
```

Then trim the relevant container into `tests/test_parsers.py` and adjust the selector until
the test passes. Keep fixtures small — a whole page is 40–90 KB, and only the one table
matters.

Two layouts exist for materials and both must keep working: tables wrapped in
`.cv-course-home-folder-container`, and a single bare table with no folders at all.
