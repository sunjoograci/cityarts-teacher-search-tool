from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.db import group_teacher_rows


def test_group_teacher_rows_keeps_directory_url_with_the_email_bearing_row():
    """A teacher listed at two sibling campuses (e.g. a shared district
    roster) merges into one row — directory_url must come from the SAME
    school row as the email/title, not just whichever campus was listed
    first, so the merged row's link actually points at where the contact
    info was found."""
    rows = [
        {
            "teacher_name": "Jane Smith", "district_name": "Example ISD", "state": "TX",
            "school_name": "North Elementary", "city": "Example", "id": 1,
            "title": None, "email": None, "resolution_method": "unresolved",
            "website_url": "https://exampleisd.org", "directory_url": "https://exampleisd.org/north/staff",
        },
        {
            "teacher_name": "Jane Smith", "district_name": "Example ISD", "state": "TX",
            "school_name": "South Elementary", "city": "Example", "id": 2,
            "title": "Art Teacher", "email": "jsmith@exampleisd.org", "resolution_method": "scraped",
            "website_url": "https://exampleisd.org", "directory_url": "https://exampleisd.org/south/staff",
        },
    ]
    grouped = group_teacher_rows(rows)
    assert len(grouped) == 1
    row = grouped[0]
    assert row["email"] == "jsmith@exampleisd.org"
    assert row["directory_url"] == "https://exampleisd.org/south/staff"
    assert row["school_name"] == "North Elementary, South Elementary"


def test_group_teacher_rows_single_school_passthrough():
    rows = [{
        "teacher_name": "Ann Lee", "district_name": "Solo ISD", "state": "TX",
        "school_name": "Solo High", "city": "Solo", "id": 1,
        "title": "Art", "email": "alee@solo.org", "resolution_method": "scraped",
        "website_url": "https://solo.org", "directory_url": "https://solo.org/staff",
    }]
    grouped = group_teacher_rows(rows)
    assert len(grouped) == 1
    assert grouped[0]["directory_url"] == "https://solo.org/staff"
