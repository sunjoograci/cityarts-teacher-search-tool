"""
Step 3 — Email enrichment.

Resolution priority:
  1. Email already scraped → keep as-is (method: scraped)
  2. Hunter.io domain search
  3. Pattern generation + SMTP verification

Usage:
    python -m src.enrich [--limit 50]
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import smtplib
import socket
import time
import urllib.parse
from typing import Optional

import dns.resolver
import requests
from dotenv import load_dotenv

from .db import get_conn

load_dotenv()
log = logging.getLogger(__name__)

HUNTER_API_KEY = os.getenv("HUNTER_API_KEY", "")
HUNTER_DOMAIN_SEARCH_URL = "https://api.hunter.io/v2/domain-search"
HUNTER_EMAIL_FINDER_URL = "https://api.hunter.io/v2/email-finder"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _domain_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    parsed = urllib.parse.urlparse(url)
    netloc = parsed.netloc or parsed.path
    netloc = netloc.lstrip("www.").split(":")[0]
    return netloc.strip() or None


def _name_parts(full_name: str) -> tuple[str, str]:
    parts = full_name.strip().split()
    if len(parts) >= 2:
        return parts[0].lower(), parts[-1].lower()
    return parts[0].lower(), ""


def _generate_patterns(first: str, last: str, domain: str) -> list[str]:
    if not first or not last or not domain:
        return []
    f, l = first, last
    return [
        f"{f}.{l}@{domain}",
        f"{f[0]}{l}@{domain}",
        f"{f}{l}@{domain}",
        f"{f}_{l}@{domain}",
        f"{f}@{domain}",
        f"{l}@{domain}",
        f"{f[0]}.{l}@{domain}",
    ]


# ---------------------------------------------------------------------------
# Hunter.io
# ---------------------------------------------------------------------------

def _hunter_find_email(first: str, last: str, domain: str) -> Optional[str]:
    if not HUNTER_API_KEY:
        return None
    try:
        resp = requests.get(
            HUNTER_EMAIL_FINDER_URL,
            params={"first_name": first, "last_name": last, "domain": domain, "api_key": HUNTER_API_KEY},
            timeout=10,
        )
        data = resp.json()
        email = data.get("data", {}).get("email")
        if email:
            log.debug("Hunter found: %s", email)
        return email
    except Exception as exc:
        log.debug("Hunter error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# SMTP verification
# ---------------------------------------------------------------------------

def _get_mx(domain: str) -> Optional[str]:
    try:
        records = dns.resolver.resolve(domain, "MX")
        mx = sorted(records, key=lambda r: r.preference)[0]
        return str(mx.exchange).rstrip(".")
    except Exception:
        return None


def _smtp_verify(email: str) -> bool:
    domain = email.split("@")[1]
    mx = _get_mx(domain)
    if not mx:
        return False
    try:
        with smtplib.SMTP(timeout=10) as smtp:
            smtp.connect(mx, 25)
            smtp.helo("cityarts.org")
            smtp.mail("verify@cityarts.org")
            code, _ = smtp.rcpt(email)
            return code == 250
    except (smtplib.SMTPException, socket.error, OSError):
        return False


# ---------------------------------------------------------------------------
# Main enrichment loop
# ---------------------------------------------------------------------------

def enrich_staff(limit: int | None = None) -> None:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.id, s.teacher_name, s.school_id, sc.website_url, sc.state
            FROM staff s
            JOIN schools sc ON sc.id = s.school_id
            WHERE s.resolution_method = 'unresolved'
            ORDER BY s.id
            """
            + (f" LIMIT {limit}" if limit else "")
        ).fetchall()

    log.info("Enriching %d unresolved contacts…", len(rows))

    for i, row in enumerate(rows, 1):
        row = dict(row)
        staff_id = row["id"]
        name = row["teacher_name"]
        domain = _domain_from_url(row.get("website_url") or "")
        first, last = _name_parts(name)

        log.info("[%d/%d] %s (domain: %s)", i, len(rows), name, domain)

        resolved_email = None
        method = "unresolved"

        # 2. Hunter.io
        if domain and HUNTER_API_KEY:
            resolved_email = _hunter_find_email(first, last, domain)
            if resolved_email:
                method = "hunter"

        # 3. Pattern + SMTP
        if not resolved_email and domain:
            patterns = _generate_patterns(first, last, domain)
            for pattern in patterns:
                log.debug("  SMTP verify: %s", pattern)
                if _smtp_verify(pattern):
                    resolved_email = pattern
                    method = "smtp_verified"
                    log.info("  SMTP confirmed: %s", pattern)
                    break
                time.sleep(0.5)

        if resolved_email:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE staff SET email=?, resolution_method=? WHERE id=?",
                    (resolved_email, method, staff_id),
                )
            log.info("  → %s via %s", resolved_email, method)
        else:
            log.info("  → unresolved")

        # Be polite
        if i < len(rows):
            time.sleep(1.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Enrich teacher emails.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    enrich_staff(args.limit)


if __name__ == "__main__":
    main()
