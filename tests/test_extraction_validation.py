"""
Regression coverage for the name/title/directory-content validators in
src/scraper.py. These exist specifically to stop nav-menu labels and
board-document titles from being saved as if they were real teachers — a
real bug found by auditing extraction output (e.g. "Federal & Special
Programs" saved as a "teacher name" identically across five schools because
a district-cache-poisoned URL kept getting reused).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scraper import (
    _looks_like_name,
    _looks_like_title,
    _looks_like_staff_directory,
    _normalize_last_first,
    _parse_table_row,
    _parse_multi_row_table,
)


def test_rejects_nav_and_document_labels_as_names():
    bad = [
        "Federal & Special Programs", "Special Programs", "School Board",
        "Job Postings", "Student Organizations", "USED OSER Requirements",
        "Board Agenda / Minutes", "Health/Kinesiology & PE",
        "Vehicle Maintenance", "Fine Arts", "Elementary Academics",
        "South Bus Terminal", "2023-2024 Board Meeting Agenda",
        "Toggle Fine Arts Section",
    ]
    for name in bad:
        assert _looks_like_name(name) is False, f"{name!r} should be rejected"


def test_accepts_real_names():
    good = [
        "Kayla Kinser", "John Vela", "Scott Lowe", "Mary O'Brien",
        "Jean-Paul Sartre", "J. Smith Jones",
    ]
    for name in good:
        assert _looks_like_name(name) is True, f"{name!r} should be accepted"


def test_accepts_all_caps_names():
    # Many real school directories display names in ALL CAPS — this must
    # not be confused with an institutional acronym (AISD, USED, OSER).
    # An earlier version of the name-word regex rejected these outright.
    good = ["KATE REECE", "JORDANN DREHER", "CHRISTINE RAMIREZ", "AVERY AYERS-BERRY"]
    for name in good:
        assert _looks_like_name(name) is True, f"{name!r} should be accepted"


def test_rejects_paragraph_length_titles():
    paragraph = (
        "The Bells ISD District Departments serve as the foundation of our "
        "school community, providing the essential services and strategic "
        "oversight necessary for student success."
    )
    assert _looks_like_title(paragraph) is False


def test_accepts_real_titles():
    for title in ("Art Teacher", "HS Art Teacher", "Band Director", "Theatre/GT"):
        assert _looks_like_title(title) is True


def test_staff_directory_content_check_rejects_bare_mentions():
    # A board-policy page that merely mentions "staff" once and has one
    # email address should NOT be mistaken for a staff directory.
    html = """
    <html><body>
    <p>This policy applies to staff and students. Contact: info@example.org</p>
    </body></html>
    """
    assert _looks_like_staff_directory(html) is False


def test_staff_directory_content_check_accepts_real_directory():
    html = "<html><body>" + "".join(
        f'<div><a href="mailto:teacher{i}@example.org">Teacher {i}</a></div>'
        for i in range(5)
    ) + "</body></html>"
    assert _looks_like_staff_directory(html) is True


def test_staff_directory_content_check_accepts_name_dense_page():
    names = [
        "Jane Smith", "John Doe", "Mary Johnson", "Robert Brown",
        "Linda Davis", "Michael Wilson",
    ]
    html = "<html><body>" + "".join(f"<p>{n}</p>" for n in names) + "</body></html>"
    assert _looks_like_staff_directory(html) is True


# ---------------------------------------------------------------------------
# Multi-row table parsing — a real structural bug: a whole staff table
# matched as ONE DOM element (not per-person rows) caused the single-record
# fallback to blend two different people's rows into one bogus record, e.g.
# name="Limon, Sarah\tCounselor HS\tEmail", title="Livingston, John\tTeacher
# HS Art\tEmail" — Sarah Limon's row supplied the "name" and John
# Livingston's row (the next one alphabetically) supplied the "title".
# ---------------------------------------------------------------------------

def test_normalize_last_first():
    assert _normalize_last_first("Livingston, John") == "John Livingston"
    assert _normalize_last_first("Kayla Kinser") == "Kayla Kinser"  # no comma, unchanged
    assert _normalize_last_first(", John") == ", John"  # malformed, left as-is rather than guessed


def test_parse_table_row():
    assert _parse_table_row("Livingston, John\tTeacher HS Art\tEmail") == ("John Livingston", "Teacher HS Art")
    assert _parse_table_row("just one cell") is None
    assert _parse_table_row("2024-2025 Board Meeting\tAgenda") is None  # fails name validation


def test_parse_multi_row_table_recovers_real_people_without_cross_contamination():
    lines = [
        "Limon, Sarah\tCounselor HS\tEmail",
        "Livingston, John\tTeacher HS Art\tEmail",
        "Potts, Christy\tRegistrar\tEmail",
        "Prather, Joseph\tTeacher HS CTE Art, AV Technology\tEmail",
    ]
    rows = _parse_multi_row_table(lines)
    assert ("Sarah Limon", "Counselor HS") in rows
    assert ("John Livingston", "Teacher HS Art") in rows
    # The historical bug: "Sarah Limon" must never end up paired with
    # John Livingston's title, or vice versa.
    names = [r[0] for r in rows]
    titles = [r[1] for r in rows]
    assert "Sarah Limon" in names and "Counselor HS" in titles
    assert not any(name == "Sarah Limon" and title != "Counselor HS" for name, title in rows)


def test_parse_multi_row_table_requires_at_least_two_rows():
    # A single stray tab-delimited line shouldn't trigger "whole table"
    # handling — that's the normal single-record path's job.
    assert _parse_multi_row_table(["Livingston, John\tTeacher HS Art\tEmail"]) == []


def test_parse_multi_row_table_ignores_non_row_lines():
    lines = [
        "Staff Directory",  # page heading, not a row
        "Livingston, John\tTeacher HS Art\tEmail",
        "Prather, Joseph\tTeacher HS CTE Art\tEmail",
    ]
    rows = _parse_multi_row_table(lines)
    assert len(rows) == 2
    assert all(name != "Staff Directory" for name, _ in rows)
