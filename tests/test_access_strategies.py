from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.access_strategies import (
    find_pagination,
    build_paginated_urls,
    find_letter_anchors,
    looks_like_staff_json,
    looks_like_auth_wall,
    looks_like_bot_challenge,
    ExponentialBackoff,
)


def test_find_pagination():
    hrefs = [
        "https://x.org/staff?page=1",
        "https://x.org/staff?page=2",
        "https://x.org/staff?page=5",
        "https://x.org/about",
    ]
    param, max_page, base = find_pagination(hrefs)
    assert param == "page"
    assert max_page == 5
    assert base == "https://x.org/staff"


def test_find_pagination_none():
    assert find_pagination(["https://x.org/about"]) == (None, 1, None)


def test_build_paginated_urls():
    urls = build_paginated_urls("page", 3, "https://x.org/staff")
    assert urls == ["https://x.org/staff?page=2", "https://x.org/staff?page=3"]


def test_find_letter_anchors_burnham_wood_style():
    html = "".join(f"<a href='#{c}'>{c}</a>" for c in "BFGHJLOPRSTVZ")
    letters = find_letter_anchors(html)
    assert letters == list("BFGHJLOPRSTVZ")


def test_looks_like_staff_json():
    assert looks_like_staff_json([{"name": "Jane Smith", "title": "Art Teacher"}]) is True
    assert looks_like_staff_json([{"unrelated": "field"}]) is False
    assert looks_like_staff_json({"results": [{"first_name": "Jane"}]}) is True
    assert looks_like_staff_json([]) is False


def test_auth_wall_detection():
    assert looks_like_auth_wall("Please log in to view the staff directory") is True
    assert looks_like_auth_wall("", has_password_field=True) is True
    assert looks_like_auth_wall("Welcome to our school") is False


def test_bot_challenge_detection():
    assert looks_like_bot_challenge(403, "") is True
    assert looks_like_bot_challenge(200, "Checking your browser before accessing") is True
    assert looks_like_bot_challenge(200, "Welcome!") is False


def test_exponential_backoff_doubles_and_resets():
    b = ExponentialBackoff(base_delay=1.0, max_delay=10.0)
    assert b.record_failure() == 1.0
    assert b.record_failure() == 2.0
    assert b.record_failure() == 4.0
    b.record_success()
    assert b.record_failure() == 1.0


def test_exponential_backoff_caps_at_max():
    b = ExponentialBackoff(base_delay=1.0, max_delay=3.0)
    for _ in range(5):
        b.record_failure()
    assert b._delay <= 3.0
