"""Fetch everything from MCV into the local snapshot, recording what changed."""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

from .client import MCVClient, MCVError
from .config import Config
from .parsers import (
    parse_active_panel,
    parse_assignments,
    parse_grades,
    parse_materials,
    parse_worksheet_description,
)
from .store import Store

log = logging.getLogger(__name__)


class Syncer:
    def __init__(self, client: MCVClient, store: Store, config: Config) -> None:
        self._client = client
        self._store = store
        self._config = config

    def sync_once(self, full: bool = False) -> dict[str, Any]:
        """Refresh the snapshot. `full` walks every semester, not just the current one."""
        run_id = self._store.start_sync()
        # On the very first run everything would look "new"; backfill quietly instead.
        record_events = not self._store.is_first_sync
        counts: dict[str, Any] = {}

        try:
            self._client.ensure_login()
            semesters = self._client.get_semesters()
            if not semesters:
                raise MCVError("no year/semester options found on the MCV home page")

            targets = semesters if full else semesters[:1]
            courses: list[dict[str, Any]] = []
            for yearsem in targets:
                for course in self._client.get_courses(yearsem):
                    cv_cid = str(course.get("cv_cid") or "").strip()
                    if not cv_cid:
                        continue
                    courses.append(
                        {
                            "cv_cid": cv_cid,
                            "course_no": course.get("course_no") or "",
                            "title": course.get("title") or "",
                            "yearsem": yearsem,
                            "raw": str(course),
                        }
                    )

            counts["courses"] = self._store.upsert(
                "courses", courses, record_events=record_events
            )

            assignments: list[dict[str, Any]] = []
            materials: list[dict[str, Any]] = []
            grades: list[dict[str, Any]] = []

            for course in courses:
                cv_cid = course["cv_cid"]
                materials.extend(
                    self._safely(
                        lambda: parse_materials(self._client.get_course_page(cv_cid), cv_cid),
                        cv_cid,
                        "materials",
                    )
                )
                assignments.extend(
                    self._safely(
                        lambda: parse_assignments(
                            self._client.get_course_page(cv_cid, "assignment"), cv_cid
                        ),
                        cv_cid,
                        "assignments",
                    )
                )
                grades.extend(
                    self._safely(
                        lambda: parse_grades(self._client.get_portfolio_page(cv_cid), cv_cid),
                        cv_cid,
                        "grades",
                    )
                )

            # The dashboard panel is the reliable source for "what is due soon", so fold it
            # in for anything the per-course tabs missed.
            known = {a["id"] for a in assignments}
            for item in parse_active_panel(self._client.get_active_panel_html()):
                row = {
                    "id": f"{item['cv_cid']}:{item['item_id']}",
                    "cv_cid": item["cv_cid"],
                    "item_id": str(item["item_id"]),
                    "title": item["title"],
                    "description": item["description"],
                    "due_at": item["due_at"],
                    "due_text": item["due_text"],
                    "url": item["url"],
                }
                if row["id"] not in known:
                    assignments.append(row)
                    known.add(row["id"])

            self._fill_descriptions(assignments, refetch=full)

            counts["assignments"] = self._store.upsert(
                "assignments", assignments, record_events=record_events
            )
            counts["materials"] = self._store.upsert(
                "materials", materials, record_events=record_events
            )
            counts["grades"] = self._store.upsert(
                "grades", grades, record_events=record_events
            )

            self._store.mark_initial_sync_done()
            self._store.finish_sync(run_id, ok=True, counts=counts)
            log.info("sync ok: %s", counts)
            return {"ok": True, "backfill": not record_events, "counts": counts}

        except Exception as exc:  # surfaced through auth_status / sync_now
            self._store.finish_sync(run_id, ok=False, error=str(exc), counts=counts)
            log.warning("sync failed: %s", exc)
            return {"ok": False, "error": str(exc), "counts": counts}

    def _safely(self, fetch, cv_cid: str, what: str) -> list[dict[str, Any]]:
        """One course failing (no access, MCV hiccup) must not abort the whole sync."""
        try:
            return fetch()
        except MCVError as exc:
            log.warning("course %s: could not read %s: %s", cv_cid, what, exc)
            return []

    def _fill_descriptions(
        self, assignments: list[dict[str, Any]], refetch: bool = False
    ) -> None:
        """Add each assignment's instruction text, fetched from its worksheet page.

        The description lives on a separate page, so it costs one request per assignment.
        Only assignments we have not seen before are fetched, which makes a steady-state
        poll zero extra requests. Note an empty description is a real answer (MCV shows
        "-- No instruction has been given --" for many assignments), so it must be cached
        too, or those would be re-fetched forever. `refetch` (a full sync) picks up
        instructions added after an assignment was first posted.
        """
        known = {
            row["id"]: row["description"]
            for row in self._store.query("SELECT id, description FROM assignments")
        }

        for row in assignments:
            if not refetch and row["id"] in known:
                row["description"] = known[row["id"]]
                continue
            if not str(row.get("item_id", "")).isdigit():
                continue
            try:
                html = self._client.get_worksheet_page(row["cv_cid"], row["item_id"])
            except MCVError as exc:
                log.debug("no worksheet for %s: %s", row["id"], exc)
                row["description"] = known.get(row["id"], "")
                continue
            row["description"] = parse_worksheet_description(html)


class Poller:
    """Background refresh so new items are noticed even with no chat open."""

    def __init__(self, syncer: Syncer, interval_minutes: int) -> None:
        self._syncer = syncer
        self._interval = max(10, interval_minutes) * 60
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="mcv-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 1
        while not self._stop.is_set():
            result = self._syncer.sync_once()
            if result.get("ok"):
                backoff = 1
                delay = self._interval
            else:
                backoff = min(backoff * 2, 8)
                delay = self._interval * backoff
            # Jitter so repeated restarts do not line up into a burst.
            self._stop.wait(delay + random.uniform(0, 30))


def run_forever(config: Config) -> None:
    """Standalone poller entry point (python -m mcv_mcp.poller)."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = Store(config.db_path)
    with MCVClient(config) as client:
        syncer = Syncer(client, store, config)
        while True:
            syncer.sync_once()
            time.sleep(max(10, config.poll_minutes) * 60)
