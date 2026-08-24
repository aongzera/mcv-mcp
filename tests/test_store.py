"""The diff/event log is what `whats_new` depends on, so pin its behaviour down."""

from __future__ import annotations

import pytest

from mcv_mcp.store import Store


@pytest.fixture()
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


def _assignment(item_id: str, **overrides):
    row = {
        "id": f"100:{item_id}",
        "cv_cid": "100",
        "item_id": item_id,
        "title": f"Homework {item_id}",
        "description": "",
        "due_at": "2026-09-01T12:00:00+00:00",
        "due_text": "due 1 Sep 2026 12:00",
        "url": f"/course/100/item/{item_id}",
    }
    row.update(overrides)
    return row


def test_new_rows_are_added_and_logged(store):
    counts = store.upsert("assignments", [_assignment("1"), _assignment("2")])

    assert counts == {"added": 2, "changed": 0, "unchanged": 0}
    events = store.events_since("2000-01-01T00:00:00+00:00")
    assert {e["kind"] for e in events} == {"assignment_new"}
    assert len(events) == 2


def test_reobserving_identical_rows_is_not_a_change(store):
    store.upsert("assignments", [_assignment("1")])
    counts = store.upsert("assignments", [_assignment("1")])

    assert counts == {"added": 0, "changed": 0, "unchanged": 1}
    # Still only the original creation event - no churn.
    assert len(store.events_since("2000-01-01T00:00:00+00:00")) == 1


def test_edited_due_date_is_reported_as_changed(store):
    store.upsert("assignments", [_assignment("1")])
    counts = store.upsert(
        "assignments", [_assignment("1", due_at="2026-09-05T12:00:00+00:00")]
    )

    assert counts == {"added": 0, "changed": 1, "unchanged": 0}
    kinds = [e["kind"] for e in store.events_since("2000-01-01T00:00:00+00:00")]
    assert "assignment_changed" in kinds

    row = store.query("SELECT due_at FROM assignments WHERE id = '100:1'")[0]
    assert row["due_at"] == "2026-09-05T12:00:00+00:00"


def test_first_sync_backfills_without_events(store):
    assert store.is_first_sync is True

    store.upsert("assignments", [_assignment("1")], record_events=False)
    store.mark_initial_sync_done()

    assert store.is_first_sync is False
    assert store.events_since("2000-01-01T00:00:00+00:00") == []

    # Anything arriving after the backfill is genuinely new.
    store.upsert("assignments", [_assignment("2")])
    events = store.events_since("2000-01-01T00:00:00+00:00")
    assert len(events) == 1
    assert events[0]["ref_id"] == "100:2"


def test_events_can_be_filtered_by_kind(store):
    store.upsert("assignments", [_assignment("1")])
    store.upsert(
        "grades",
        [
            {
                "id": "100:mid",
                "cv_cid": "100",
                "item_id": "mid",
                "item_title": "Midterm",
                "score": "42",
                "max_score": "50",
            }
        ],
    )

    only_grades = store.events_since("2000-01-01T00:00:00+00:00", kinds=["grade_new"])
    assert len(only_grades) == 1
    # The score is the point of a grade event, so it belongs in the summary.
    assert only_grades[0]["summary"] == "Midterm: 42 / 50"


def test_grade_release_is_reported_as_a_change(store):
    """'Not ready' -> a number is exactly the "my grade was uploaded" signal."""

    def graded(score):
        return {
            "id": "100:mid",
            "cv_cid": "100",
            "item_id": "mid",
            "item_title": "Midterm",
            "score": score,
            "max_score": "50",
        }

    store.upsert("grades", [graded("Not ready")], record_events=False)
    counts = store.upsert("grades", [graded("42")])

    assert counts == {"added": 0, "changed": 1, "unchanged": 0}
    events = store.events_since("2000-01-01T00:00:00+00:00", kinds=["grade_changed"])
    assert events[0]["summary"] == "Midterm: 42 / 50"


def test_courses_are_keyed_by_cv_cid(store):
    """`courses` has no `id` column - it is keyed on MCV's own course id."""
    course = {"cv_cid": "100", "course_no": "2110471", "title": "Networks", "yearsem": "2026/1"}

    assert store.upsert("courses", [course]) == {"added": 1, "changed": 0, "unchanged": 0}
    assert store.upsert("courses", [course]) == {"added": 0, "changed": 0, "unchanged": 1}

    renamed = {**course, "title": "Computer Networks I"}
    assert store.upsert("courses", [renamed]) == {"added": 0, "changed": 1, "unchanged": 0}
    assert store.query("SELECT title FROM courses")[0]["title"] == "Computer Networks I"


def test_announcements_are_tracked_like_everything_else(store):
    ann = {
        "id": "100:9",
        "cv_cid": "100",
        "item_id": "9",
        "title": "Quiz moved to Friday",
        "body": "",
        "posted_at": "2026-08-17T17:00:00+00:00",
        "posted_text": "18 Aug 26",
        "url": "https://mcv.test/course/100/view_content_node_9",
    }
    assert store.upsert("announcements", [ann])["added"] == 1
    events = store.events_since("2000-01-01T00:00:00+00:00")
    assert [e["kind"] for e in events] == ["announcement_new"]
    assert events[0]["summary"] == "Quiz moved to Friday"

    # An edited body (the syncer refetches on full sync) is a change event.
    counts = store.upsert("announcements", [{**ann, "body": "Now in room 301."}])
    assert counts == {"added": 0, "changed": 1, "unchanged": 0}


def test_quiet_tables_backfill_without_events(store):
    """A table introduced after the initial sync backfills silently via quiet_tables."""
    ann = {
        "id": "100:9",
        "cv_cid": "100",
        "item_id": "9",
        "title": "Old announcement",
        "body": "",
        "posted_at": None,
        "posted_text": "01 Jul 26",
        "url": "https://mcv.test/course/100/view_content_node_9",
    }
    store.apply_sync({"announcements": [ann]}, quiet_tables=("announcements",))
    assert store.events_since("2000-01-01T00:00:00+00:00") == []

    # Later rows arrive with events on again.
    newer = {**ann, "id": "100:10", "item_id": "10", "title": "Fresh news"}
    store.apply_sync({"announcements": [ann, newer]})
    events = store.events_since("2000-01-01T00:00:00+00:00")
    assert [e["ref_id"] for e in events] == ["100:10"]


def test_sync_run_records_failure(store):
    run_id = store.start_sync()
    store.finish_sync(run_id, status="error", error="boom")

    last = store.last_sync()
    assert last["ok"] == 0
    assert last["status"] == "error"
    assert last["error"] == "boom"
    assert last["finished_at"] is not None
