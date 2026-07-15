"""
Regression suite — the six ground-truth entity-resolution cases from the
CityArts pipeline rebuild spec. Each represents a distinct failure class the
old scraper silently swallowed as "no directory found."

Run: python -m pytest tests/test_regression.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.entity_resolver import resolve, build_containment_key


def test_neisd_is_a_district():
    r = resolve("NEISD", state_hint="TX")
    assert r.entity_type == "DISTRICT"
    assert "north east" in r.resolved_name.lower()
    assert len(r.children) > 50  # ~70 campuses
    assert r.parent_entity is None  # real ISD, no charter-holding parent


def test_burnham_wood_is_a_district_with_parent_org():
    r = resolve("Burnham Wood", state_hint="TX")
    assert r.entity_type == "DISTRICT"
    assert "burnham wood" in r.resolved_name.lower()
    names = {c.lower() for c in r.children}
    assert any("davinci" in n or "da vinci" in n for n in names)  # NCES spells it "DAVINCI"
    assert any("howard burnham" in n for n in names)
    assert r.parent_entity == "El Paso Education Initiative"
    assert r.parent_entity_type == "PARENT_ORG"
    assert r.needs_human_review  # parent-org fact unconfirmed, per its own note


def test_data_at_adams_is_a_program_not_a_terminal_school():
    r = resolve("Digital Arts and Technology Academy at Adams Middle School", state_hint="TX")
    assert r.entity_type == "PROGRAM"
    assert r.parent_entity == "John Adams Middle School"
    assert r.domain == "adams.gpisd.org"
    # must not collide with the unrelated Albuquerque charter of a similar name
    assert r.domain != "datacharter.org"
    assert not r.needs_human_review  # host-school signal disambiguates it


def test_tca_and_a_is_a_parent_org_not_a_school():
    r = resolve("Texas Center for Arts + Academics")
    assert r.entity_type == "PARENT_ORG"
    assert r.domain == "artsacademics.org"
    children = {c.lower() for c in r.children}
    assert any("fort worth academy of fine arts" in c for c in children)
    assert any("texas school of the arts" in c for c in children)


def test_fwafa_is_a_school_child_of_tca_and_a():
    r = resolve("Fort Worth Academy of Fine Arts", state_hint="TX")
    assert r.entity_type == "SCHOOL"
    assert r.nces_id is not None
    assert r.parent_entity == "Texas Center for Arts + Academics"
    assert r.parent_entity_type == "PARENT_ORG"


def test_mangled_texas_school_of_fine_arts_resolves_to_tesa():
    r = resolve("Texas School of Fine Arts", state_hint="TX")
    assert r.entity_type == "SCHOOL"
    assert r.resolved_name == "Texas School Of The Arts"
    assert r.parent_entity == "Texas Center for Arts + Academics"
    assert r.resolution_confidence < 1.0  # fuzzy match, not exact
    assert "austin" in r.resolution_note.lower()  # false-positive collision noted


def test_tca_and_a_subtree_dedupes_to_one_crawl_target():
    """TCA+A, FWAFA, and TeSA are the same organization — three input rows
    must collapse into one crawl, not three."""
    org = resolve("Texas Center for Arts + Academics")
    fwafa = resolve("Fort Worth Academy of Fine Arts", state_hint="TX")
    tesa = resolve("Texas School of Fine Arts", state_hint="TX")  # mangled name
    keys = {build_containment_key(org), build_containment_key(fwafa), build_containment_key(tesa)}
    assert len(keys) == 1


def test_unrelated_albuquerque_charter_is_not_confused_with_the_program():
    """Sanity check for the name-collision failure class: the *other*
    'Digital Arts and Technology Academy' (Albuquerque, NM) must resolve on
    its own terms, not get merged into the Adams MS program."""
    r = resolve("Digital Arts and Technology Academy", state_hint="NM")
    assert r.entity_type != "PROGRAM"
    assert r.domain != "adams.gpisd.org"
