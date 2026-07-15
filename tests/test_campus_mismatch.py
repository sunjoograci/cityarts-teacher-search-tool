"""
District-wide roster misattribution — see the module note above
_drop_mismatched_campus_records in src/scraper.py for the full root-cause
explanation. Found by auditing production data: a shared multi-campus
roster page (reused across sibling campuses via district_dir_cache)
attributed every row to every campus scraped, not just the one each row's
title actually names.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.scraper import (
    StaffRecord,
    _campus_tag_matches_school,
    _drop_mismatched_campus_records,
    _extract_campus_tag,
)


def test_extract_campus_tag_leading():
    assert _extract_campus_tag("Landry - Art") == "Landry"
    assert _extract_campus_tag("McKamy - Art Teacher") == "McKamy"


def test_extract_campus_tag_trailing():
    assert _extract_campus_tag("Art Teacher - FJH") == "FJH"
    assert _extract_campus_tag("Art Teacher - DPJH") == "DPJH"
    assert _extract_campus_tag("Teacher - Digital Art/3D Modeling - SC") == "SC"


def test_extract_campus_tag_none_for_ordinary_titles():
    assert _extract_campus_tag("Art Teacher") is None
    assert _extract_campus_tag("Director of Fine Arts") is None
    assert _extract_campus_tag("K-12 Art Teacher") is None
    assert _extract_campus_tag("AP Art History") is None
    assert _extract_campus_tag("") is None


def test_campus_tag_matches_school():
    assert _campus_tag_matches_school("Landry", "LANDRY ELEMENTARY SCHOOL") is True
    assert _campus_tag_matches_school("McKamy", "MCKAMY MIDDLE") is True
    assert _campus_tag_matches_school("FJH", "FAIRMONT JUNIOR HIGH") is True
    assert _campus_tag_matches_school("Landry", "BLALACK MIDDLE") is False
    assert _campus_tag_matches_school("FJH", "DEER PARK JUNIOR HIGH") is False


def test_drop_mismatched_campus_records_removes_other_campuses():
    records = [
        StaffRecord(name="Jade Melton", title="McKamy - Art"),
        StaffRecord(name="Christen Packard", title="Rosemeade - Art"),
        StaffRecord(name="David Carroll", title="Art"),  # untagged, always kept
    ]
    kept = _drop_mismatched_campus_records(records, "MCKAMY MIDDLE")
    names = {r.name for r in kept}
    assert names == {"Jade Melton", "David Carroll"}


def test_drop_mismatched_campus_records_needs_two_distinct_tags():
    # A single tagged record, with no second distinct tag alongside it, is
    # not enough evidence this page mixes campuses — leave it alone rather
    # than risk a false-positive drop on an isolated, coincidental "X - Y"
    # title shape.
    records = [StaffRecord(name="Jade Melton", title="McKamy - Art")]
    kept = _drop_mismatched_campus_records(records, "BLALACK MIDDLE")
    assert len(kept) == 1


def test_drop_mismatched_campus_records_noop_when_no_tags():
    records = [
        StaffRecord(name="A Teacher", title="Art Teacher"),
        StaffRecord(name="B Teacher", title="Visual Arts Instructor"),
    ]
    kept = _drop_mismatched_campus_records(records, "ANY SCHOOL")
    assert len(kept) == 2
