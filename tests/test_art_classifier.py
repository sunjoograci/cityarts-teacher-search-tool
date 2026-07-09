from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.art_classifier import is_art_related, extract_discipline, classify


def test_language_arts_never_matches():
    assert is_art_related("3rd Grade Language Arts") is False
    assert is_art_related("Language Arts Teacher") is False
    assert is_art_related("ELA Coordinator") is False


def test_org_name_arts_plus_academics_never_matches():
    assert is_art_related("Arts + Academics") is False
    assert is_art_related("Texas Center for Arts and Academics") is False


def test_other_exclusions():
    assert is_art_related("Martial Arts Instructor") is False
    assert is_art_related("Culinary Arts Teacher") is False
    assert is_art_related("Liberal Arts Advisor") is False
    assert is_art_related("Industrial Arts Teacher") is False
    assert is_art_related("Dean of Arts and Sciences") is False
    assert is_art_related("Arts Integration Coordinator") is False


def test_true_positives():
    assert is_art_related("Visual Arts Teacher") is True
    assert is_art_related("Ceramics Instructor") is True
    assert is_art_related("Band Director") is True
    assert is_art_related("Theatre Teacher") is True
    assert is_art_related("Choir Director") is True
    assert is_art_related("AP Art History") is True


def test_mixed_string_still_matches_the_art_part():
    assert is_art_related("Visual Arts / Language Arts Co-Chair") is True


def test_discipline_extraction():
    assert extract_discipline("Visual Arts Teacher") == "visual art"
    assert extract_discipline("Theatre Teacher") == "theatre"
    assert extract_discipline("Dance Instructor") == "dance"
    assert extract_discipline("Choir Director") == "music"
    assert extract_discipline("Digital Media Teacher") == "media"
    assert extract_discipline("Fine Arts") == "unknown"


def test_multi_role_staff():
    c = classify("Art Teacher / Yearbook Advisor")
    assert c.is_art is True
    assert c.discipline == "visual art"


def test_classify_returns_full_record():
    c = classify("3rd Grade Language Arts")
    assert c.is_art is False
    assert c.matched_term is None
