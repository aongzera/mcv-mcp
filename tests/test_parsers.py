"""Parser tests.

The materials, assignment, grade and worksheet fixtures below are trimmed from real pages
captured from an authenticated session (see docs/ENDPOINTS.md), so they exercise the actual
selectors. The active-panel fixture is still synthetic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from mcv_mcp.parsers import (
    is_released,
    parse_active_panel,
    parse_assignments,
    parse_due_text,
    parse_grades,
    parse_materials,
    parse_worksheet_description,
)

NOW = datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc)


class TestDueText:
    def test_relative_span_resolves_against_now(self):
        due_at, text = parse_due_text("dues in 2 days", NOW)
        assert due_at == "2026-08-17T12:00:00+00:00"
        assert text == "dues in 2 days"

    def test_combined_units(self):
        due_at, _ = parse_due_text("dues in 1 day 6 hours", NOW)
        assert due_at == "2026-08-16T18:00:00+00:00"

    def test_absolute_date_is_read_as_bangkok_time(self):
        # MCV renders Bangkok local time (UTC+7) and never labels it as such.
        due_at, _ = parse_due_text("2026-09-01 23:59", NOW)
        assert due_at == "2026-09-01T16:59:00+00:00"

    def test_screen_reader_phrasing(self):
        due_at, text = parse_due_text("Due on 23 August 2026 at 23:59", NOW)
        assert due_at == "2026-08-23T16:59:00+00:00"
        assert text == "Due on 23 August 2026 at 23:59"

    def test_date_without_a_time(self):
        due_at, _ = parse_due_text("Out on 17 August 2026", NOW)
        assert due_at == "2026-08-16T17:00:00+00:00"  # midnight Bangkok

    def test_unparseable_keeps_original_text(self):
        due_at, text = parse_due_text("sometime after the exam", NOW)
        assert due_at is None
        assert text == "sometime after the exam"

    def test_empty(self):
        assert parse_due_text("", NOW) == (None, "")


ACTIVE_PANEL = """
<ul>
  <li>
    <a target="_blank" href="/course/34207/item/991">
      <strong>&ldquo;Lab 3: Linked Lists&rdquo;</strong> dues in 2 days in <strong>Data Structures</strong>
    </a>
  </li>
  <li>
    <a target="_blank" href="/course/34999/item/1002">
      <strong>&ldquo;Essay draft&rdquo;</strong> dues in 6 hours in <strong>Academic Writing</strong>
    </a>
  </li>
</ul>
"""


class TestActivePanel:
    def test_extracts_each_assignment(self):
        rows = parse_active_panel(ACTIVE_PANEL, NOW)
        assert len(rows) == 2

        first = rows[0]
        assert first["cv_cid"] == "34207"
        assert first["item_id"] == "991"
        assert first["title"] == "Lab 3: Linked Lists"
        assert first["due_at"] == "2026-08-17T12:00:00+00:00"

    def test_quotes_are_stripped_from_titles(self):
        titles = [r["title"] for r in parse_active_panel(ACTIVE_PANEL, NOW)]
        assert titles == ["Lab 3: Linked Lists", "Essay draft"]

    def test_empty_panel(self):
        assert parse_active_panel("", NOW) == []


# Trimmed from a real course home page: material tables inside a collapsible folder.
MATERIALS = """
<section id="courseville-material-list">
 <div class="cv-course-home-folder-container" data-folder="tid-1"
      aria-label="Course materials in folder titled Lecture Slides">
  <button class="cv-course-home-folder-control">
    <div><div data-part="title">Lecture Slides</div><div data-part="num">(Containing 2 items)</div></div>
  </button>
  <table class="cv-course-home-material-table">
   <tbody>
    <tr>
     <td data-col="thumbnail"><img class="cv-course-home-material-thumb"/></td>
     <td data-col="title">
      <a aria-label="View material titled Chapter 1: Introduction [PDF]"
         href="?q=courseville/course/86405/view_content_node_2085487_material">Chapter 1: Introduction [PDF]</a>
     </td>
     <td data-col="action">
      <a href="https://mycourseville-default.s3.ap-southeast-1.amazonaws.com/f/ch1.pdf">
       <span class="courseville-download-file" data-nid="2085487"></span>
      </a>
     </td>
    </tr>
    <tr>
     <td data-col="thumbnail"><img class="cv-course-home-material-thumb"/></td>
     <td data-col="title">
      <a aria-label="View material titled Chapter 1: Introduction [PPTX]"
         href="?q=courseville/course/86405/view_content_node_2085486_material">Chapter 1: Introduction [PPTX]</a>
     </td>
     <td data-col="action">
      <a href="https://mycourseville-default.s3.ap-southeast-1.amazonaws.com/f/ch1.pptx">
       <span class="courseville-download-file" data-nid="2085486"></span>
      </a>
     </td>
    </tr>
   </tbody>
  </table>
 </div>
</section>
"""

# Other courses have no folders at all - just a bare table.
MATERIALS_FLAT = """
<section id="courseville-material-list">
 <table class="cv-course-home-material-table">
  <tbody>
   <tr>
    <td data-col="title">
     <a aria-label="View material titled Class 03: React Framework"
        href="?q=courseville/course/87509/view_content_node_2135465_material">Class 03: React Framework</a>
    </td>
    <td data-col="action">
     <a href="https://mycourseville-default.s3.ap-southeast-1.amazonaws.com/f/class03.pdf">
      <span class="courseville-download-file" data-nid="2135465"></span>
     </a>
    </td>
   </tr>
  </tbody>
 </table>
</section>
"""


class TestMaterials:
    def test_reads_name_folder_and_download_url(self):
        rows = parse_materials(MATERIALS, "86405")
        assert len(rows) == 2

        first = rows[0]
        assert first["name"] == "Chapter 1: Introduction [PDF]"
        assert first["folder"] == "Lecture Slides"
        assert first["url"].endswith("/f/ch1.pdf")
        assert first["cv_cid"] == "86405"

    def test_id_uses_the_content_node_id(self):
        rows = parse_materials(MATERIALS, "86405")
        assert [r["id"] for r in rows] == ["86405:2085487", "86405:2085486"]

    def test_id_is_stable(self):
        assert [r["id"] for r in parse_materials(MATERIALS, "86405")] == [
            r["id"] for r in parse_materials(MATERIALS, "86405")
        ]

    def test_folderless_layout(self):
        rows = parse_materials(MATERIALS_FLAT, "87509")
        assert len(rows) == 1
        assert rows[0]["folder"] == ""
        assert rows[0]["name"] == "Class 03: React Framework"

    def test_relative_urls_are_made_absolute(self):
        html = (
            '<table class="cv-course-home-material-table"><tr>'
            '<td data-col="title"><a aria-label="View material titled Notes" '
            'href="?q=courseville/course/1/view_content_node_9_material">Notes</a></td>'
            "</tr></table>"
        )
        url = parse_materials(html, "1")[0]["url"]
        assert url.startswith("https://www.mycourseville.com/?q=")

    def test_slashes_in_names_do_not_break_ids(self):
        html = (
            '<table class="cv-course-home-material-table"><tr>'
            '<td data-col="title"><a aria-label="View material titled A/B test notes" '
            'href="/x">x</a></td>'
            '<td data-col="action"><a href="/d/1.pdf">d</a></td></tr></table>'
        )
        assert "/" not in parse_materials(html, "1")[0]["name"]

    def test_empty(self):
        assert parse_materials("", "1") == []


# Trimmed from a real /assignment page.
ASSIGNMENTS = """
<table id="cv-assignment-table">
 <thead><th></th><th>Title</th><th>Out Date</th><th>Due Date</th></thead>
 <tbody>
  <tr>
   <td><img class="cv-assignment-marker"/></td>
   <td><a href="?q=courseville/worksheet/86405/2104433" target="_blank">Review Questions #2</a>
       <div class="sr-only">This assignment is for each individual student</div></td>
   <td><span class="courseville-post-date"><div class="inner">Aug</div>17<div>2026</div></span>
       <div class="sr-only">Out on 17 August 2026</div></td>
   <td class="cv-due-col">
       <span class="courseville-post-date"><div class="inner">Aug</div>23<div>2026</div></span>
       <div class="sr-only">Due on 23 August 2026 at 23:59</div></td>
  </tr>
 </tbody>
</table>
"""


class TestAssignments:
    def test_reads_title_id_and_deadline(self):
        rows = parse_assignments(ASSIGNMENTS, "86405", NOW)
        assert len(rows) == 1

        row = rows[0]
        assert row["id"] == "86405:2104433"
        assert row["item_id"] == "2104433"
        assert row["title"] == "Review Questions #2"
        assert row["due_at"] == "2026-08-23T16:59:00+00:00"  # 23:59 Bangkok
        assert row["due_text"] == "Due on 23 August 2026 at 23:59"
        assert row["url"].endswith("?q=courseville/worksheet/86405/2104433")

    def test_visible_date_stack_is_ignored(self):
        # The visible cell renders as 'Aug 23 2026' split across divs; only the
        # screen-reader copy is unambiguous, so that is what must be used.
        assert parse_assignments(ASSIGNMENTS, "86405", NOW)[0]["due_at"] is not None

    def test_description_is_left_for_the_worksheet_fetch(self):
        assert parse_assignments(ASSIGNMENTS, "86405", NOW)[0]["description"] == ""

    def test_no_table(self):
        assert parse_assignments("<html><body>nothing</body></html>", "1", NOW) == []

    def test_empty(self):
        assert parse_assignments("", "1", NOW) == []


class TestWorksheetDescription:
    def test_reads_instruction_body(self):
        html = (
            '<div id="courseville-worksheet-instruction-body">'
            "<p>Answer all questions in Chapter 2.</p></div>"
        )
        assert parse_worksheet_description(html) == "Answer all questions in Chapter 2."

    def test_placeholder_becomes_empty(self):
        html = (
            '<div id="courseville-worksheet-instruction-body">'
            "-- No instruction has been given --</div>"
        )
        assert parse_worksheet_description(html) == ""

    def test_missing(self):
        assert parse_worksheet_description("<html></html>") == ""


# Trimmed from a real portfolio page.
GRADES = """
<table id="courseville-portfolio-gradeditem-table">
 <thead><th>Item</th><th>Points obtained</th><th>from [total points]</th></thead>
 <tbody>
  <tr class="courseville-point-table-total-row">
   <td>Current total</td><td><span class="courseville-cal-point"></span></td>
   <td><span>/ 100</span><div class="sr-only">from 100</div></td>
  </tr>
  <tr class="courseville-even" content_id="2097057" id="row-0">
   <td>[MIDTERM] Signal</td>
   <td><span title="Entered points">18.50</span></td>
   <td><span>/ 30</span><div class="sr-only">from 30</div></td>
  </tr>
  <tr class="courseville-odd" content_id="2097056" id="row-1">
   <td>[QUIZ] Optimization</td>
   <td><span title="Entered points">Not ready</span></td>
   <td><span>/ 25</span><div class="sr-only">from 25</div></td>
  </tr>
 </tbody>
</table>
"""


class TestGrades:
    def test_reads_item_score_and_max(self):
        rows = parse_grades(GRADES, "87118")
        assert len(rows) == 2  # 'Current total' is not a graded item

        first = rows[0]
        assert first["id"] == "87118:2097057"
        assert first["item_title"] == "[MIDTERM] Signal"
        assert first["score"] == "18.50"
        assert first["max_score"] == "30"

    def test_unreleased_items_are_kept(self):
        # Keeping them is what lets the next sync report the release as a change.
        quiz = parse_grades(GRADES, "87118")[1]
        assert quiz["score"] == "Not ready"
        assert is_released(quiz["score"]) is False
        assert is_released("18.50") is True

    def test_generic_fallback_for_other_score_tables(self):
        html = (
            "<table><thead><tr><th>Item</th><th>Score</th></tr></thead>"
            "<tbody><tr><td>Midterm</td><td>42 / 50</td></tr></tbody></table>"
        )
        rows = parse_grades(html, "1")
        assert rows[0]["score"] == "42"
        assert rows[0]["max_score"] == "50"

    def test_ignores_tables_without_a_score_column(self):
        html = (
            "<table><thead><tr><th>Name</th><th>Email</th></tr></thead>"
            "<tbody><tr><td>Alice</td><td>a@b.c</td></tr></tbody></table>"
        )
        assert parse_grades(html, "1") == []

    def test_empty(self):
        assert parse_grades("", "1") == []
