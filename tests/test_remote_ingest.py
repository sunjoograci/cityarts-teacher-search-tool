"""
Central ingest endpoint — tests that a remote-mode local scrape's POST
correctly upserts a school (by nces_id) and its staff into the receiving
server's database, independent of any real browser/network scraping.

Run: python -m pytest tests/test_remote_ingest.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "ingest_test.db"))
    monkeypatch.delenv("SCRAPE_SHARED_SECRET", raising=False)
    # src.db reads DB_PATH at import time, so force a fresh import per test.
    for mod in ("src.db", "app"):
        sys.modules.pop(mod, None)
    import app as app_module
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


_PAYLOAD = {
    "school": {
        "nces_id": "999999000001",
        "school_name": "Test Middle School",
        "city": "Testville",
        "state": "tx",
        "district_name": "Test ISD",
        "website_url": "https://testisd.example.org",
        "school_level": "2",
        "is_arts_school": False,
    },
    "status": "OK",
    "directory_url": "https://testisd.example.org/staff",
    "paths_attempted": [{"url": "https://testisd.example.org/staff", "status": 200, "rejected_because": "MATCHED"}],
    "resolution": {
        "entity_type": "SCHOOL",
        "parent_entity": None,
        "parent_entity_type": None,
        "resolution_confidence": 1.0,
        "resolution_note": "exact CCD match",
        "domain": "testisd.example.org",
        "needs_human_review": False,
    },
    "records": [
        {
            "name": "JANE DOE",
            "title": "Art Teacher",
            "email": "jdoe@testisd.example.org",
            "discipline": "visual art",
            "email_source": "MAILTO",
            "email_verified": False,
            "evidence_url": "https://testisd.example.org/staff",
            "extraction_strategy": "semantic_card",
        },
    ],
}


def test_ingest_creates_school_and_staff(client):
    resp = client.post("/api/ingest/school", json=_PAYLOAD)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True
    assert body["saved"] == 1

    from src.db import get_conn
    with get_conn() as conn:
        school = conn.execute(
            "SELECT * FROM schools WHERE nces_id=?", (_PAYLOAD["school"]["nces_id"],)
        ).fetchone()
        assert school is not None
        assert school["state"] == "TX"  # normalized to uppercase
        assert school["scraped"] == 1
        assert school["scrape_status"] == "OK"

        staff = conn.execute(
            "SELECT * FROM staff WHERE school_id=?", (school["id"],)
        ).fetchone()
        assert staff is not None
        assert staff["teacher_name"] == "Jane Doe"  # recased on the way in
        assert staff["email"] == "jdoe@testisd.example.org"
        assert staff["extraction_strategy"] == "semantic_card"


def test_ingest_is_idempotent_and_upserts_same_school(client):
    client.post("/api/ingest/school", json=_PAYLOAD)
    resp = client.post("/api/ingest/school", json=_PAYLOAD)
    assert resp.status_code == 200
    assert resp.get_json()["saved"] == 0  # UNIQUE(school_id, teacher_name) -> no dupe

    from src.db import get_conn
    with get_conn() as conn:
        n_schools = conn.execute(
            "SELECT COUNT(*) FROM schools WHERE nces_id=?", (_PAYLOAD["school"]["nces_id"],)
        ).fetchone()[0]
        n_staff = conn.execute("SELECT COUNT(*) FROM staff").fetchone()[0]
    assert n_schools == 1
    assert n_staff == 1


def test_ingest_requires_nces_id(client):
    bad = {**_PAYLOAD, "school": {**_PAYLOAD["school"], "nces_id": ""}}
    resp = client.post("/api/ingest/school", json=bad)
    assert resp.status_code == 400


def test_ingest_rejects_wrong_secret(client, monkeypatch):
    monkeypatch.setenv("SCRAPE_SHARED_SECRET", "correct-horse")
    sys.modules.pop("app", None)
    import app as app_module
    app_module.app.config["TESTING"] = True
    c = app_module.app.test_client()

    resp = c.post("/api/ingest/school", json=_PAYLOAD)
    assert resp.status_code == 401

    resp = c.post(
        "/api/ingest/school", json=_PAYLOAD,
        headers={"X-Scrape-Secret": "correct-horse"},
    )
    assert resp.status_code == 200
