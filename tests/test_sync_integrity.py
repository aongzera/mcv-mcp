"""Regression tests for the two sync defects observed 2026-08-23.

1. The sync-run record must always be finalized - success, failure, or even a
   BaseException killing the thread mid-run.
2. Readers must never observe a partially-applied sync: mid-sync reads return the
   previous complete snapshot, and the row count never shrinks-then-grows across
   a sync boundary.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from mcv_mcp import sync as sync_mod
from mcv_mcp.client import MCVError
from mcv_mcp.store import Store
from mcv_mcp.sync import Syncer


def _assignment(i: int) -> dict:
    return {
        "id": f"111:{i}",
        "cv_cid": "111",
        "item_id": str(i),
        "title": f"HW{i}",
        "description": "",
        "due_at": None,
        "due_text": "soon",
        "url": f"https://mcv.test/worksheet/{i}",
    }


class FakeClient:
    """Just enough of MCVClient for Syncer; page HTML is decoded by patched parsers."""

    def __init__(self, assignments: list[dict] | None = None) -> None:
        self.assignments = assignments or []

    def ensure_login(self) -> None:
        pass

    def get_semesters(self) -> list[str]:
        return ["2569/1"]

    def get_courses(self, yearsem: str) -> list[dict]:
        return [{"cv_cid": "111", "course_no": "2100000", "title": "Testing 101"}]

    def get_course_page(self, cv_cid: str, tab: str | None = None) -> str:
        return "<html>"

    def get_portfolio_page(self, cv_cid: str) -> str:
        return "<html>"

    def get_active_panel_html(self) -> str:
        return "<html>"

    def get_worksheet_page(self, cv_cid: str, item_id: str) -> str:
        raise MCVError("no worksheets in tests")


@pytest.fixture
def patch_parsers(monkeypatch):
    monkeypatch.setattr(sync_mod, "parse_materials", lambda html, cv_cid: [])
    monkeypatch.setattr(sync_mod, "parse_grades", lambda html, cv_cid: [])
    monkeypatch.setattr(sync_mod, "parse_active_panel", lambda html: [])
    monkeypatch.setattr(sync_mod, "parse_worksheet_description", lambda html: "")


def _make(tmp_path, monkeypatch, client: FakeClient) -> tuple[Store, Syncer]:
    store = Store(tmp_path / "mcv.db")
    # The fake pages all parse to whatever the fake client currently holds.
    monkeypatch.setattr(
        sync_mod,
        "parse_assignments",
        lambda html, cv_cid: [dict(r) for r in client.assignments],
    )
    return store, Syncer(client, store, config=None)


# --------------------------------------------------------- defect 1: finalization


def test_success_finalizes_run(tmp_path, monkeypatch, patch_parsers):
    store, syncer = _make(tmp_path, monkeypatch, FakeClient([_assignment(1), _assignment(2)]))

    result = syncer.sync_once()

    assert result["ok"] is True
    last = store.last_sync()
    assert last["finished_at"] is not None
    assert last["status"] == "ok"
    assert last["ok"] == 1
    counts = json.loads(last["counts"])
    assert counts["assignments"]["added"] == 2


def test_failure_finalizes_run(tmp_path, monkeypatch, patch_parsers):
    class Failing(FakeClient):
        def get_semesters(self) -> list[str]:
            raise MCVError("MCV is down")

    store, syncer = _make(tmp_path, monkeypatch, Failing())

    result = syncer.sync_once()

    assert result["ok"] is False
    last = store.last_sync()
    assert last["finished_at"] is not None
    assert last["status"] == "error"
    assert "MCV is down" in last["error"]


def test_base_exception_still_finalizes_run(tmp_path, monkeypatch, patch_parsers):
    """A daemon thread dying mid-run (process shutdown, Ctrl-C) must not leave the
    record looking forever 'running'."""

    class Killed(FakeClient):
        def get_active_panel_html(self) -> str:
            raise KeyboardInterrupt

    store, syncer = _make(tmp_path, monkeypatch, Killed())

    with pytest.raises(KeyboardInterrupt):
        syncer.sync_once()

    last = store.last_sync()
    assert last["finished_at"] is not None
    assert last["status"] == "error"
    assert "interrupted" in last["error"]


def test_new_sync_marks_orphaned_running_run_stale(tmp_path):
    store = Store(tmp_path / "mcv.db")
    first = store.start_sync()  # crashed process: never finalized
    second = store.start_sync()

    rows = {r["id"]: r for r in store.query("SELECT * FROM sync_runs")}
    assert rows[first]["status"] == "stale"
    assert rows[second]["status"] == "running"


def test_migration_backfills_status_for_old_unfinalized_rows(tmp_path):
    """A pre-status DB with a never-finalized row (the observed run #15) must come
    out labeled 'stale', not silently indistinguishable from running."""
    db = tmp_path / "mcv.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE sync_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT, "
        "finished_at TEXT, ok INTEGER, error TEXT, counts TEXT)"
    )
    conn.execute("INSERT INTO sync_runs(started_at, ok) VALUES('2026-08-23T15:26:30+00:00', 0)")
    conn.commit()
    conn.close()

    store = Store(db)
    assert store.last_sync()["status"] == "stale"


def test_sync_status_flag(tmp_path):
    store = Store(tmp_path / "mcv.db")
    assert store.sync_status() == {"sync_in_progress": False, "last_completed_sync": None}

    run = store.start_sync()
    assert store.sync_status()["sync_in_progress"] is True

    store.finish_sync(run, status="ok", counts={})
    status = store.sync_status()
    assert status["sync_in_progress"] is False
    assert status["last_completed_sync"] is not None


def test_concurrent_sync_in_same_process_is_refused(tmp_path, monkeypatch, patch_parsers):
    _, syncer = _make(tmp_path, monkeypatch, FakeClient())
    assert syncer._sync_lock.acquire(blocking=False)
    try:
        result = syncer.sync_once()
    finally:
        syncer._sync_lock.release()
    assert result["ok"] is False
    assert result["in_progress"] is True


# ------------------------------------------------------ defect 2: read isolation


def test_reader_never_sees_partial_sync(tmp_path, monkeypatch, patch_parsers):
    """Assignment count must not shrink-then-grow (or show intermediate values)
    across a sync boundary: a reader sees the old complete snapshot until the
    whole new one is committed."""
    client = FakeClient([_assignment(i) for i in range(1, 6)])
    store, syncer = _make(tmp_path, monkeypatch, client)
    reader = Store(tmp_path / "mcv.db")  # its own connection, like another process

    def count() -> int:
        return reader.query("SELECT COUNT(*) AS n FROM assignments")[0]["n"]

    assert syncer.sync_once()["ok"] is True
    assert count() == 5

    # Second sync grows the set to 8; sample the reader's view at the two danger
    # points: mid-fetch, and mid-apply while the write transaction is open (the old
    # code had already committed the assignments table at that point).
    client.assignments = [_assignment(i) for i in range(1, 9)]
    samples: list[int] = []

    real_upsert = store._upsert_rows

    def spying_upsert(table, rows, *, record_events=True):
        result = real_upsert(table, rows, record_events=record_events)
        if table == "assignments":
            samples.append(count())
        return result

    monkeypatch.setattr(store, "_upsert_rows", spying_upsert)

    real_panel = client.get_active_panel_html
    client.get_active_panel_html = lambda: (samples.append(count()), real_panel())[1]

    assert syncer.sync_once()["ok"] is True

    assert samples == [5, 5], "mid-sync reads must return the previous complete snapshot"
    assert count() == 8
