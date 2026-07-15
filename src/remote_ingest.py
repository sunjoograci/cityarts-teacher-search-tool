"""
Remote persistence for a local scrape whose results should land in a
shared central database instead of (or in addition to) this process's own
local one — the mechanism behind "run the scraper on your own machine, get
the residential-IP yield, but the team still sees one shared dataset."

`build_remote_persist_fn` returns a `persist_fn` with the exact signature
`run_scraper` calls (see its docstring), so it's a drop-in replacement for
the default local-write behavior — nothing else about the scraping engine
changes.
"""
from __future__ import annotations

import logging

from .db import get_conn
from .scraper import record_to_dict

log = logging.getLogger(__name__)


def build_remote_persist_fn(ingest_url: str, secret: str | None):
    """`ingest_url` is the central server's base URL (e.g.
    "http://129.153.189.11:8080"); `secret` is sent as the X-Scrape-Secret
    header if the server has SCRAPE_SHARED_SECRET configured (harmless to
    send even if it doesn't)."""
    endpoint = ingest_url.rstrip("/") + "/api/ingest/school"
    headers = {"X-Scrape-Secret": secret} if secret else {}

    async def persist(
        school_dict, records, status, paths_attempted, resolution,
        directory_url, session,
    ) -> int:
        payload = {
            "school": {
                "nces_id": school_dict.get("nces_id"),
                "school_name": school_dict.get("school_name"),
                "city": school_dict.get("city"),
                "state": school_dict.get("state"),
                "district_name": school_dict.get("district_name"),
                "website_url": school_dict.get("website_url"),
                "school_level": school_dict.get("school_level"),
                "is_arts_school": school_dict.get("is_arts_school"),
            },
            "status": status,
            "directory_url": directory_url,
            "paths_attempted": paths_attempted,
            "resolution": {
                "entity_type": resolution.entity_type,
                "parent_entity": resolution.parent_entity,
                "parent_entity_type": resolution.parent_entity_type,
                "resolution_confidence": resolution.resolution_confidence,
                "resolution_note": resolution.resolution_note,
                "domain": resolution.domain,
                "needs_human_review": resolution.needs_human_review,
            },
            "records": [record_to_dict(r) for r in records],
        }
        saved = 0
        try:
            async with session.post(endpoint, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    log.warning(
                        "Remote ingest failed for %s: HTTP %d %s",
                        school_dict.get("school_name"), resp.status, body[:200],
                    )
                else:
                    data = await resp.json()
                    saved = int(data.get("saved", 0))
        except Exception as exc:
            log.warning("Remote ingest error for %s: %s", school_dict.get("school_name"), exc)

        # This machine's own local schools table still tracks scraped=1 so
        # a repeated local run doesn't redo the same schools every time —
        # only the staff records themselves go to the remote server, never
        # this local bookkeeping column.
        with get_conn() as conn:
            conn.execute(
                "UPDATE schools SET scraped=1, scrape_status=? WHERE id=?",
                (status, school_dict["id"]),
            )
        return saved

    return persist
