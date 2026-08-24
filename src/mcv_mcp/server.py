"""MCP server exposing MyCourseVille over stdio.

Tools read from the local SQLite snapshot so they answer fast and work offline; the poller
and `sync_now` are the only things that talk to MCV.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:  # mcp >= 2.0
    from mcp.server import MCPServer as _Server
except ImportError:  # mcp 1.x called it FastMCP
    from mcp.server.fastmcp import FastMCP as _Server

from . import __version__
from .client import MCVClient, MCVError
from .config import load_config
from .parsers import is_released
from .store import Store
from .sync import Poller, Syncer

log = logging.getLogger(__name__)

config = load_config()
store = Store(config.db_path)
client = MCVClient(config)
syncer = Syncer(client, store, config)

mcp = _Server("mycourseville", version=__version__)

_RELATIVE_SINCE_RE = re.compile(r"^\s*(\d+)\s*([hdwm])\w*\s*(?:ago)?\s*$", re.I)
_WORD_SINCE = {
    "today": 1,
    "yesterday": 2,
    "week": 7,
    "last week": 7,
    "month": 30,
    "last month": 30,
}


def _parse_since(since: str | None, default_days: int = 7) -> str:
    """Accept ISO dates, '3d'/'12h', or words like 'yesterday'. Returns an ISO timestamp."""
    now = datetime.now(timezone.utc)
    if not since:
        return (now - timedelta(days=default_days)).isoformat(timespec="seconds")

    text = since.strip().lower()
    if text in _WORD_SINCE:
        return (now - timedelta(days=_WORD_SINCE[text])).isoformat(timespec="seconds")

    match = _RELATIVE_SINCE_RE.match(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        delta = {
            "h": timedelta(hours=amount),
            "d": timedelta(days=amount),
            "w": timedelta(weeks=amount),
            "m": timedelta(days=30 * amount),
        }[unit]
        return (now - delta).isoformat(timespec="seconds")

    try:
        parsed = datetime.fromisoformat(since.replace("Z", "+00:00"))
    except ValueError:
        return (now - timedelta(days=default_days)).isoformat(timespec="seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat(timespec="seconds")


def _freshness() -> dict[str, Any]:
    """Staleness signal attached to every read so a mid-sync answer is never silent."""
    status = store.sync_status()
    fresh: dict[str, Any] = {
        "sync_in_progress": status["sync_in_progress"],
        "last_completed_sync": status["last_completed_sync"],
    }
    if status["sync_in_progress"]:
        fresh["warning"] = (
            "a sync is running right now; this answer reflects the previous "
            "completed sync and may be missing very recent items"
        )
    return fresh


# ---------------------------------------------------------------- what's new


@mcp.tool()
def whats_new(since: str | None = None, kinds: str | None = None, limit: int = 50) -> dict:
    """What appeared or changed on MyCourseVille recently.

    Args:
        since: ISO date, a relative span like '3d' / '12h', or 'yesterday'. Default 7 days.
        kinds: comma-separated filter, e.g. 'assignment_new,announcement_new,grade_new'.
            Default all.
        limit: max events to return.
    """
    since_iso = _parse_since(since)
    kind_list = [k.strip() for k in kinds.split(",")] if kinds else None
    events = store.events_since(since_iso, kind_list, limit)
    return {
        "since": since_iso,
        "count": len(events),
        "events": [
            {
                "kind": e["kind"],
                "what": e["summary"],
                "course": e.get("course_title") or e.get("cv_cid"),
                "course_no": e.get("course_no"),
                "detected_at": e["detected_at"],
                "ref_id": e["ref_id"],
            }
            for e in events
        ],
        "note": (
            "'detected_at' is when this server first saw the item, which is the best "
            "available proxy - MCV does not expose a reliable posted-at timestamp."
        ),
        **_freshness(),
    }


@mcp.tool()
def list_upcoming_deadlines(within_days: int = 7, include_undated: bool = True) -> dict:
    """Assignments due within the next N days, soonest first."""
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(days=within_days)).isoformat(timespec="seconds")

    rows = store.query(
        "SELECT a.*, c.title AS course_title, c.course_no "
        "FROM assignments a LEFT JOIN courses c ON c.cv_cid = a.cv_cid "
        "WHERE a.due_at IS NOT NULL AND a.due_at >= ? AND a.due_at <= ? "
        "ORDER BY a.due_at ASC",
        (now.isoformat(timespec="seconds"), horizon),
    )

    if include_undated:
        rows += store.query(
            "SELECT a.*, c.title AS course_title, c.course_no "
            "FROM assignments a LEFT JOIN courses c ON c.cv_cid = a.cv_cid "
            "WHERE a.due_at IS NULL AND a.due_text != '' ORDER BY a.last_changed DESC"
        )

    return {
        "within_days": within_days,
        "count": len(rows),
        "assignments": [_assignment(r) for r in rows],
        **_freshness(),
    }


# ------------------------------------------------------------------- reads


@mcp.tool()
def list_courses(yearsem: str | None = None) -> dict:
    """Enrolled courses. `yearsem` filters to one semester, e.g. '2568/1'."""
    if yearsem:
        rows = store.query("SELECT * FROM courses WHERE yearsem = ? ORDER BY course_no", (yearsem,))
    else:
        rows = store.query("SELECT * FROM courses ORDER BY yearsem DESC, course_no")
    return {
        "count": len(rows),
        "courses": [
            {
                "cv_cid": r["cv_cid"],
                "course_no": r["course_no"],
                "title": r["title"],
                "yearsem": r["yearsem"],
            }
            for r in rows
        ],
        **_freshness(),
    }


@mcp.tool()
def list_assignments(cv_cid: str | None = None, include_past: bool = False) -> dict:
    """Assignments with their deadline and description. `cv_cid` filters to one course."""
    sql = (
        "SELECT a.*, c.title AS course_title, c.course_no "
        "FROM assignments a LEFT JOIN courses c ON c.cv_cid = a.cv_cid"
    )
    params: list[Any] = []
    where = []
    if cv_cid:
        where.append("a.cv_cid = ?")
        params.append(str(cv_cid))
    if not include_past:
        where.append("(a.due_at IS NULL OR a.due_at >= ?)")
        params.append(datetime.now(timezone.utc).isoformat(timespec="seconds"))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY a.due_at IS NULL, a.due_at ASC"

    rows = store.query(sql, params)
    return {"count": len(rows), "assignments": [_assignment(r) for r in rows], **_freshness()}


@mcp.tool()
def get_assignment(assignment_id: str) -> dict:
    """Full detail for one assignment. `assignment_id` is '<cv_cid>:<item_id>'."""
    rows = store.query(
        "SELECT a.*, c.title AS course_title, c.course_no "
        "FROM assignments a LEFT JOIN courses c ON c.cv_cid = a.cv_cid WHERE a.id = ?",
        (assignment_id,),
    )
    if not rows:
        return {"found": False, "error": f"no assignment with id {assignment_id!r}", **_freshness()}
    return {"found": True, "assignment": _assignment(rows[0]), **_freshness()}


@mcp.tool()
def list_announcements(cv_cid: str | None = None, since: str | None = None, limit: int = 50) -> dict:
    """Course announcements posted by instructors, newest first.

    Args:
        cv_cid: filter to one course.
        since: ISO date, a relative span like '3d' / '12h', or 'yesterday'. Default: all.
        limit: max announcements to return.
    """
    sql = (
        "SELECT a.*, c.title AS course_title, c.course_no "
        "FROM announcements a LEFT JOIN courses c ON c.cv_cid = a.cv_cid"
    )
    params: list[Any] = []
    where = []
    if cv_cid:
        where.append("a.cv_cid = ?")
        params.append(str(cv_cid))
    if since:
        # posted_at can be NULL (unparseable date); fall back to when we first saw it.
        where.append("COALESCE(a.posted_at, a.first_seen) >= ?")
        params.append(_parse_since(since))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(a.posted_at, a.first_seen) DESC LIMIT ?"
    params.append(limit)

    rows = store.query(sql, params)
    return {
        "count": len(rows),
        "announcements": [_announcement(r) for r in rows],
        **_freshness(),
    }


@mcp.tool()
def get_announcement(announcement_id: str) -> dict:
    """Full detail for one announcement. `announcement_id` is '<cv_cid>:<content_id>'."""
    rows = store.query(
        "SELECT a.*, c.title AS course_title, c.course_no "
        "FROM announcements a LEFT JOIN courses c ON c.cv_cid = a.cv_cid WHERE a.id = ?",
        (announcement_id,),
    )
    if not rows:
        return {
            "found": False,
            "error": f"no announcement with id {announcement_id!r}",
            **_freshness(),
        }
    return {"found": True, "announcement": _announcement(rows[0]), **_freshness()}


@mcp.tool()
def list_materials(cv_cid: str | None = None) -> dict:
    """Course materials (files posted in MCV): folder, name and URL."""
    if cv_cid:
        rows = store.query(
            "SELECT * FROM materials WHERE cv_cid = ? ORDER BY folder, name", (str(cv_cid),)
        )
    else:
        rows = store.query("SELECT * FROM materials ORDER BY cv_cid, folder, name")
    return {
        "count": len(rows),
        "materials": [
            {
                "id": r["id"],
                "cv_cid": r["cv_cid"],
                "folder": r["folder"],
                "name": r["name"],
                "url": r["url"],
                "downloaded_to": r["local_path"],
                "first_seen": r["first_seen"],
            }
            for r in rows
        ],
        **_freshness(),
    }


@mcp.tool()
def download_material(material_id: str, extract_text: bool = True) -> dict:
    """Download one material to the local downloads folder.

    Set extract_text to pull readable text out of PDFs so it can be summarised.
    """
    rows = store.query("SELECT * FROM materials WHERE id = ?", (material_id,))
    if not rows:
        return {"ok": False, "error": f"no material with id {material_id!r}"}
    material = rows[0]

    suffix = Path(str(material["url"]).split("?")[0]).suffix or ".bin"
    safe = re.sub(r'[<>:"/\\|?*]', "-", str(material["name"]))
    dest = config.download_dir / str(material["cv_cid"]) / f"{safe}{suffix}"

    try:
        client.download(str(material["url"]), dest)
    except MCVError as exc:
        return {"ok": False, "error": str(exc)}

    store.set_material_local_path(material_id, str(dest))
    result: dict[str, Any] = {"ok": True, "path": str(dest), "bytes": dest.stat().st_size}

    if extract_text and dest.suffix.lower() == ".pdf":
        result["text"] = _pdf_text(dest)

    return result


def _pdf_text(path: Path, max_chars: int = 20000) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "(pypdf not installed - cannot extract text)"
    try:
        reader = PdfReader(str(path))
        chunks = []
        for page in reader.pages:
            chunks.append(page.extract_text() or "")
            if sum(len(c) for c in chunks) > max_chars:
                break
        return "\n".join(chunks)[:max_chars]
    except Exception as exc:
        return f"(could not extract text: {exc})"


@mcp.tool()
def list_grades(cv_cid: str | None = None, released_only: bool = False) -> dict:
    """Graded items and their scores. `cv_cid` filters to one course.

    Items whose points MCV has not published yet are included with score 'Not ready' and
    `released: false`, so you can see what is still outstanding. Set `released_only` to
    hide them.
    """
    sql = (
        "SELECT g.*, c.title AS course_title, c.course_no "
        "FROM grades g LEFT JOIN courses c ON c.cv_cid = g.cv_cid"
    )
    params: list[Any] = []
    if cv_cid:
        sql += " WHERE g.cv_cid = ?"
        params.append(str(cv_cid))
    sql += " ORDER BY g.last_changed DESC"

    rows = store.query(sql, params)
    grades = [
        {
            "id": r["id"],
            "course": r.get("course_title") or r["cv_cid"],
            "course_no": r.get("course_no"),
            "item": r["item_title"],
            "score": r["score"],
            "max_score": r["max_score"],
            "released": is_released(r["score"]),
            "first_seen": r["first_seen"],
            "last_changed": r["last_changed"],
        }
        for r in rows
    ]
    if released_only:
        grades = [g for g in grades if g["released"]]

    return {"count": len(grades), "grades": grades, **_freshness()}


# ------------------------------------------------------------- housekeeping


@mcp.tool()
def sync_now(full: bool = False) -> dict:
    """Refresh from MyCourseVille now. `full` walks every semester, not just the current."""
    return syncer.sync_once(full=full)


@mcp.tool()
def auth_status() -> dict:
    """Diagnostics: credentials present, session state, last sync result, row counts."""
    last = store.last_sync()
    counts = {
        table: store.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        for table in ("courses", "assignments", "materials", "announcements", "grades", "events")
    }
    return {
        "credentials_configured": config.has_credentials,
        "username": config.username or None,
        "session_active": client.authenticated,
        "state_dir": str(config.state_dir),
        "poll_enabled": config.poll_enabled,
        "poll_minutes": config.poll_minutes,
        "initial_sync_done": not store.is_first_sync,
        "last_sync": last,
        "row_counts": counts,
        **_freshness(),
    }


def _announcement(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "course": row.get("course_title") or row["cv_cid"],
        "course_no": row.get("course_no"),
        "title": row["title"],
        "body": row["body"],
        "posted_at": row["posted_at"],
        "posted_text": row["posted_text"],
        "url": row["url"],
        "first_seen": row["first_seen"],
    }


def _assignment(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "course": row.get("course_title") or row["cv_cid"],
        "course_no": row.get("course_no"),
        "title": row["title"],
        "description": row["description"],
        "due_at": row["due_at"],
        "due_text": row["due_text"],
        "url": row["url"],
        "first_seen": row["first_seen"],
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if config.poll_enabled and config.has_credentials:
        Poller(syncer, config.poll_minutes).start()
    mcp.run()
