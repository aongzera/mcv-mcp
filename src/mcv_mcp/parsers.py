"""HTML -> structured data.

Pure functions only: no network, no state. MCV's markup is the most likely thing to break,
so keeping it isolated here means a breakage is one fixture test away from being diagnosed.
Fixtures live in tests/fixtures/ - see docs/ENDPOINTS.md for how to capture new ones.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from bs4 import BeautifulSoup, Tag

from .config import BASE

# MCV is a Chulalongkorn service: every date it renders is Bangkok local time, and it never
# says so. Absolute dates are therefore read as UTC+7 and stored as UTC.
BANGKOK = timezone(timedelta(hours=7))

_FOLDER_TITLE_RE = re.compile(r"Course materials in folder titled\s+(.+?)\s*$", re.I)
_VIEW_MATERIAL_RE = re.compile(r"View material titled\s+(.+?)\s*$", re.I)
_COURSE_HREF_RE = re.compile(r"/course/(\d+)")
_RELATIVE_DUE_RE = re.compile(
    r"(\d+)\s*(minute|min|hour|hr|day|week|month)s?", re.I
)
_DUE_PHRASE_RE = re.compile(r"(due[sd]?\s+(?:in|on|at|by)?[^<\n]*)", re.I)
# Leading wording on MCV's screen-reader dates: "Due on 23 August 2026 at 23:59".
_DUE_PREFIX_RE = re.compile(r"^\s*(?:dues?|out)\s+(?:on|in|at|by)\s+", re.I)
_MAX_SCORE_RE = re.compile(r"(?:from|/)\s*([\d.]+)")
_WORKSHEET_ID_RE = re.compile(r"/worksheet/\d+/(\d+)")

# Score text MCV shows for an item whose points have not been released yet.
_UNRELEASED = {"not ready", "-", "", "n/a"}

_UNIT_TO_DELTA = {
    "minute": "minutes",
    "min": "minutes",
    "hour": "hours",
    "hr": "hours",
    "day": "days",
    "week": "weeks",
    "month": "days",  # approximated below
}

_ABSOLUTE_FORMATS = (
    "%d %b %Y %H:%M",
    "%d %B %Y %H:%M",
    "%d %b %Y, %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%d/%m/%Y %H:%M",
    "%d %b %Y",
    "%d %B %Y",
    "%Y-%m-%d",
)


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html or "", "html.parser")


def _clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _absolute_url(href: str | None) -> str:
    """MCV links are mostly relative ('?q=courseville/...'); make them usable."""
    url = (href or "").replace("\\", "").strip()
    if not url or url.startswith(("http://", "https://")):
        return url
    if url.startswith("?"):
        return f"{BASE}/{url}"
    return BASE + ("" if url.startswith("/") else "/") + url


def _sr_text(root: Tag | None, prefix: str) -> str:
    """MCV mirrors every date into a .sr-only div in unambiguous prose.

    The visible markup is a stack of styled divs ('Aug', '23', '2026') that concatenates
    into garbage, so the screen-reader copy is the reliable source.
    """
    if root is None:
        return ""
    for node in root.select(".sr-only"):
        text = _clean(node.get_text(" "))
        if text.lower().startswith(prefix.lower()):
            return text
    return ""


# ------------------------------------------------------------------ due dates


def parse_due_text(text: str, now: datetime | None = None) -> tuple[str | None, str]:
    """Turn MCV's due wording into (ISO-8601 or None, cleaned original text).

    Handles both absolute dates and the dashboard's relative "dues in 2 days" phrasing.
    The original string is always preserved - a parsed value we are unsure about should
    never be the only thing the user sees.
    """
    cleaned = _clean(text)
    if not cleaned:
        return None, ""

    reference = now or datetime.now(timezone.utc)

    # "Due on 23 August 2026 at 23:59" -> "23 August 2026 23:59"
    stripped = _DUE_PREFIX_RE.sub("", cleaned)
    stripped = re.sub(r"\[due\]", " ", stripped, flags=re.I)
    stripped = _clean(re.sub(r"\s+at\s+", " ", stripped, flags=re.I))

    for fmt in _ABSOLUTE_FORMATS:
        for candidate in _absolute_candidates(stripped):
            try:
                parsed = datetime.strptime(candidate, fmt)
            except ValueError:
                continue
            as_utc = parsed.replace(tzinfo=BANGKOK).astimezone(timezone.utc)
            return as_utc.isoformat(timespec="seconds"), cleaned

    matches = _RELATIVE_DUE_RE.findall(cleaned)
    if matches and re.search(r"\bin\b", cleaned, re.I):
        delta = timedelta()
        for amount, unit in matches:
            key = _UNIT_TO_DELTA[unit.lower()]
            value = int(amount)
            if unit.lower() == "month":
                value *= 30
            delta += timedelta(**{key: value})
        return (reference + delta).isoformat(timespec="seconds"), cleaned

    return None, cleaned


def _absolute_candidates(text: str) -> Iterable[str]:
    """Substrings that might be a date, longest first."""
    yield text
    match = re.search(
        r"\d{1,2}[/ -]\w+[/ -]\d{2,4}(?:,?\s+\d{1,2}:\d{2}(?::\d{2})?)?", text
    )
    if match:
        yield match.group(0)
    match = re.search(r"\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?", text)
    if match:
        yield match.group(0).replace("T", " ")


# --------------------------------------------------------------- active panel


def parse_active_panel(html: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Assignments from the dashboard's 'active' panel.

    Gives title, course id and a due *phrase*; the panel only shows relative wording, so
    treat the deadline here as approximate and prefer parse_assignments() when available.
    """
    soup = _soup(html)
    results: list[dict[str, Any]] = []

    for anchor in soup.find_all("a", href=True):
        cv_cid = _cv_cid_from_href(anchor["href"])
        if not cv_cid:
            continue

        container = _enclosing_item(anchor)
        strongs = [_clean(s.get_text()) for s in container.find_all("strong")]
        strongs = [s for s in strongs if s]
        if not strongs:
            continue

        title = strongs[0].strip("“”\"'‘’")
        text = _clean(container.get_text(" "))
        due_match = _DUE_PHRASE_RE.search(text)
        due_at, due_text = parse_due_text(due_match.group(1) if due_match else "", now)

        results.append(
            {
                "cv_cid": cv_cid,
                "item_id": _item_id_from_href(anchor["href"]) or title,
                "title": title,
                "description": "",
                "due_at": due_at,
                "due_text": due_text,
                "url": anchor["href"],
            }
        )

    return _dedupe(results)


def _enclosing_item(tag: Tag) -> Tag:
    """Walk up to the row/list-item that holds one panel entry."""
    node: Tag = tag
    for _ in range(4):
        parent = node.parent
        if not isinstance(parent, Tag):
            break
        node = parent
        if node.name in {"li", "tr", "article"}:
            return node
    return node


def _cv_cid_from_href(href: str) -> str | None:
    match = _COURSE_HREF_RE.search(href)
    if match:
        return match.group(1)
    parts = [p for p in href.split("/") if p.isdigit()]
    return parts[0] if parts else None


def _item_id_from_href(href: str) -> str | None:
    digits = [p for p in href.split("/") if p.isdigit()]
    return digits[-1] if len(digits) > 1 else None


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    out = []
    for row in rows:
        key = (str(row.get("cv_cid")), str(row.get("title")))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ------------------------------------------------------------------ materials


def parse_materials(html: str, cv_cid: str) -> list[dict[str, Any]]:
    """Course materials from a course home page, grouped by the folder MCV shows them in.

    Courses use one of two layouts: material tables nested in collapsible folder
    containers, or a single bare table with no folders at all. Both are handled; a
    folder-less course simply reports an empty folder name.
    """
    soup = _soup(html)
    section = soup.select_one("#courseville-material-list") or soup

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for table in section.select("table.cv-course-home-material-table") or section.find_all(
        "table"
    ):
        folder = _folder_name(table)
        for row in table.find_all("tr"):
            entry = _material_row(row, cv_cid, folder)
            if entry and entry["id"] not in seen:
                seen.add(entry["id"])
                results.append(entry)

    if not results:
        # Layouts that list materials outside a table at all.
        for anchor in soup.find_all(attrs={"aria-label": _VIEW_MATERIAL_RE}):
            name = _VIEW_MATERIAL_RE.search(str(anchor.get("aria-label", "")))
            href = anchor.get("href")
            if name and href:
                results.append(_material(cv_cid, "", _clean(name.group(1)), str(href)))

    return results


def _folder_name(table: Tag) -> str:
    container = table.find_parent(class_="cv-course-home-folder-container")
    if container is None:
        # Also accept the synthetic `data-folder` shape used in tests and older layouts.
        node: Tag | None = table
        for _ in range(5):
            if node is None:
                break
            if node.has_attr("data-folder"):
                return _clean(str(node["data-folder"]))
            node = node.parent if isinstance(node.parent, Tag) else None
        return ""

    title = container.select_one('[data-part="title"]')
    if title is not None:
        name = _clean(title.get_text())
        if name:
            return name

    match = _FOLDER_TITLE_RE.search(str(container.get("aria-label", "")))
    if match:
        return _clean(match.group(1))
    return _clean(str(container.get("data-folder", "")))


def _material_row(row: Tag, cv_cid: str, folder: str) -> dict[str, Any] | None:
    title_cell = row.find("td", attrs={"data-col": "title"})
    link = title_cell.find("a", href=True) if isinstance(title_cell, Tag) else None

    name = ""
    label_holder = row.find(attrs={"aria-label": _VIEW_MATERIAL_RE})
    if label_holder is not None:
        match = _VIEW_MATERIAL_RE.search(str(label_holder.get("aria-label", "")))
        if match:
            name = _clean(match.group(1))
    if not name and link is not None:
        name = _clean(link.get_text())

    # The action cell holds the real file link (usually straight to S3); the title cell
    # only links to MCV's viewer page.
    action = row.find("td", attrs={"data-col": "action"})
    download = action.find("a", href=True) if isinstance(action, Tag) else None
    source = download or link or row.find("a", href=True)
    if source is None or not name:
        return None

    # `data-nid` is MCV's own content-node id - a far stabler key than folder+name.
    marker = row.find(attrs={"data-nid": True})
    node_id = _clean(str(marker["data-nid"])) if marker is not None else ""

    return _material(cv_cid, folder, name, str(source["href"]), node_id)


def _material(
    cv_cid: str, folder: str, name: str, url: str, node_id: str = ""
) -> dict[str, Any]:
    safe_name = name.replace("/", "-").replace("\\", "-")
    key = node_id or f"{folder}:{safe_name}"
    return {
        "id": f"{cv_cid}:{key}",
        "cv_cid": cv_cid,
        "folder": folder,
        "name": safe_name,
        "url": _absolute_url(url),
        "local_path": None,
    }


# ---------------------------------------------------- assignments and grades


def parse_assignments(html: str, cv_cid: str, now: datetime | None = None) -> list[dict[str, Any]]:
    """Assignments from a course's `/assignment` page.

    Descriptions are not on this page - they live on the per-assignment worksheet, so the
    field is left empty here and filled in by the syncer via parse_worksheet_description().
    """
    soup = _soup(html)
    table = soup.select_one("#cv-assignment-table")
    if table is None:
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in table.find_all("tr"):
        link = row.find("a", href=_WORKSHEET_ID_RE)
        if link is None:
            continue
        href = str(link["href"])
        match = _WORKSHEET_ID_RE.search(href)
        item_id = match.group(1) if match else ""
        title = _clean(link.get_text())
        if not item_id or not title or item_id in seen:
            continue
        seen.add(item_id)

        due_cell = row.find("td", class_="cv-due-col")
        due_at, due_text = parse_due_text(_sr_text(due_cell, "due"), now)
        posted_text = _sr_text(row, "out on")

        results.append(
            {
                "id": f"{cv_cid}:{item_id}",
                "cv_cid": cv_cid,
                "item_id": item_id,
                "title": title,
                "description": "",
                "due_at": due_at,
                "due_text": due_text or posted_text,
                "url": _absolute_url(href),
            }
        )

    return results


def parse_worksheet_description(html: str) -> str:
    """The instruction text from an assignment's worksheet page."""
    soup = _soup(html)
    body = soup.select_one("#courseville-worksheet-instruction-body")
    if body is None:
        return ""
    text = _clean(body.get_text(" "))
    # MCV's placeholder for an assignment with no written instructions.
    if text.strip("- ").lower().startswith("no instruction has been given"):
        return ""
    return text


def parse_grades(html: str, cv_cid: str) -> list[dict[str, Any]]:
    """Graded items from a course's portfolio page.

    Items whose points are not out yet are included, with score 'Not ready'. That is
    deliberate: keeping them means the release shows up as a *change* on the next sync,
    which is the signal for "my grade was uploaded".
    """
    soup = _soup(html)
    table = soup.select_one("#courseville-portfolio-gradeditem-table")
    if table is None:
        return _parse_grades_generic(soup, cv_cid)

    results: list[dict[str, Any]] = []
    for index, row in enumerate(table.find_all("tr")):
        classes = row.get("class") or []
        if "courseville-point-table-total-row" in classes:
            continue  # the course total, not a graded item

        cells = row.find_all("td")
        if len(cells) < 2:
            continue
        item_title = _clean(cells[0].get_text(" "))
        if not item_title:
            continue

        score = _clean(cells[1].get_text(" "))
        max_score = None
        if len(cells) > 2:
            match = _MAX_SCORE_RE.search(_clean(cells[2].get_text(" ")))
            if match:
                max_score = match.group(1)
        if max_score is None:
            score, max_score = _split_score(score)

        item_id = _clean(str(row.get("content_id") or row.get("id") or "")) or f"row{index}"
        results.append(
            {
                "id": f"{cv_cid}:{item_id}",
                "cv_cid": cv_cid,
                "item_id": item_id,
                "item_title": item_title,
                "score": score,
                "max_score": max_score,
            }
        )

    return results


def _parse_grades_generic(soup: BeautifulSoup, cv_cid: str) -> list[dict[str, Any]]:
    """Fallback for any table that looks like a score table."""
    results: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = [_clean(th.get_text()).lower() for th in table.find_all("th")]
        if not any("score" in h or "grade" in h or "point" in h for h in headers):
            continue
        for index, row in enumerate(table.find_all("tr")):
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            item_title = _clean(cells[0].get_text())
            if not item_title:
                continue
            score, max_score = _split_score(_clean(cells[1].get_text()))
            item_id = _clean(str(row.get("data-itemid") or "")) or f"row{index}"
            results.append(
                {
                    "id": f"{cv_cid}:{item_id}",
                    "cv_cid": cv_cid,
                    "item_id": item_id,
                    "item_title": item_title,
                    "score": score,
                    "max_score": max_score,
                }
            )
    return results


def is_released(score: str | None) -> bool:
    """False while MCV is still showing a placeholder instead of actual points."""
    return _clean(score).lower() not in _UNRELEASED


def _split_score(text: str) -> tuple[str, str | None]:
    match = re.match(r"\s*([\d.]+)\s*/\s*([\d.]+)", text)
    if match:
        return match.group(1), match.group(2)
    return text, None


def _first_text(root: Tag, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            found = root.select_one(selector)
        except Exception:  # invalid selector for this soup flavour
            found = None
        if found is not None:
            value = _clean(found.get_text(" "))
            if value:
                return value
    return ""
