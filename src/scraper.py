"""
Step 2 — Staff directory scraper using Playwright.

Usage:
    python -m src.scraper --states TX KS [--limit 10]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
import urllib.parse
import urllib.robotparser
from typing import NamedTuple

from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout

from .db import get_conn

log = logging.getLogger(__name__)

# Paths to try when hunting for a staff directory
DIRECTORY_PATHS = [
    "/staff",
    "/faculty",
    "/staff-directory",
    "/faculty-directory",
    "/contact",
    "/about/staff",
    "/about/faculty",
    "/directory",
    "/administration",
    "/our-staff",
    "/meet-our-staff",
    "/teachers",
    "/about-us/staff",
]

# Art-related keywords in job title (case-insensitive)
ART_TITLE_KEYWORDS = re.compile(
    r"\b(art|visual arts?|studio|drawing|painting|ceramics?|sculpture|photography|printmaking|graphic design)\b",
    re.IGNORECASE,
)

# Exclude "art" that appears to be part of a proper name (e.g. "Arthur")
ART_FALSE_POSITIVES = re.compile(r"\b(arthur|arturo|eart|mcart|smart)\b", re.IGNORECASE)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

DELAY_BETWEEN_REQUESTS = 2.5  # seconds


class StaffRecord(NamedTuple):
    name: str
    title: str
    email: str | None


def _is_art_teacher(title: str) -> bool:
    if not title:
        return False
    if ART_FALSE_POSITIVES.search(title):
        art_match = ART_TITLE_KEYWORDS.search(title)
        if art_match and art_match.group().lower() in ("arthur", "arturo"):
            return False
    return bool(ART_TITLE_KEYWORDS.search(title))


def _can_fetch(url: str) -> bool:
    """Check robots.txt. Returns True if crawling is allowed."""
    parsed = urllib.parse.urlparse(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(robots_url)
    try:
        rp.read()
        return rp.can_fetch("*", url)
    except Exception:
        return True  # if robots.txt unreadable, proceed


async def _find_directory_page(page: Page, base_url: str) -> str | None:
    """Try known paths and look for staff-related links on homepage."""
    for path in DIRECTORY_PATHS:
        candidate = base_url.rstrip("/") + path
        try:
            resp = await page.goto(candidate, timeout=12000, wait_until="domcontentloaded")
            await asyncio.sleep(0.5)
            if resp and resp.status < 400:
                text = await page.inner_text("body")
                # Check this page has actual staff names (look for email or name-like patterns)
                if EMAIL_RE.search(text) or re.search(r"\b(teacher|instructor|staff|faculty)\b", text, re.I):
                    log.info("  Found directory at %s", candidate)
                    return candidate
        except PWTimeout:
            pass
        except Exception as exc:
            log.debug("  Path %s: %s", path, exc)
        await asyncio.sleep(0.3)

    # Fall back: look for links on homepage
    try:
        await page.goto(base_url, timeout=15000, wait_until="domcontentloaded")
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent}))",
        )
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "").lower()
            if any(kw in text for kw in ("staff", "faculty", "directory", "teachers", "contact")):
                if href.startswith(base_url) or href.startswith("/"):
                    full = href if href.startswith("http") else base_url.rstrip("/") + href
                    log.info("  Found possible directory link: %s", full)
                    return full
    except Exception as exc:
        log.debug("  Homepage link scan failed: %s", exc)

    return None


async def _extract_staff(page: Page) -> list[StaffRecord]:
    """Extract staff names, titles and emails from the current page."""
    text = await page.inner_text("body")
    emails_on_page: set[str] = set(EMAIL_RE.findall(text))

    records: list[StaffRecord] = []

    # Strategy 1: Look for structured elements (tables, list items, cards)
    # Try to find rows/cards with both a name and a title
    candidates = await page.eval_on_selector_all(
        # Common staff card/row patterns
        "tr, li, .staff-member, .faculty-member, .teacher, [class*='staff'], [class*='faculty'], [class*='employee'], [class*='person'], [class*='card']",
        """els => els.map(el => ({
            text: el.innerText,
            emails: [...el.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:',''))
        }))""",
    )

    for item in candidates:
        raw = item.get("text", "").strip()
        item_emails = item.get("emails", [])
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        name_candidate = lines[0]
        title_candidate = " ".join(lines[1:3])
        if not _is_art_teacher(title_candidate):
            continue
        # Avoid pure-noise names
        if len(name_candidate.split()) < 2 or len(name_candidate) > 60:
            continue
        email = item_emails[0] if item_emails else None
        if not email:
            # Try to match an email near this block
            block_emails = EMAIL_RE.findall(raw)
            email = block_emails[0] if block_emails else None
        records.append(StaffRecord(name=name_candidate, title=title_candidate, email=email))

    # Strategy 2: If structured approach found nothing, fall back to mailto links with surrounding text
    if not records:
        mailto_links = await page.eval_on_selector_all(
            "a[href^='mailto:']",
            """els => els.map(el => ({
                email: el.href.replace('mailto:',''),
                text: el.closest('tr,li,div,p') ? el.closest('tr,li,div,p').innerText : el.parentElement.innerText
            }))""",
        )
        for link in mailto_links:
            email = link.get("email", "")
            surrounding = link.get("text", "")
            lines = [l.strip() for l in surrounding.splitlines() if l.strip()]
            for i, line in enumerate(lines):
                if _is_art_teacher(line):
                    name = lines[i - 1] if i > 0 else ""
                    if len(name.split()) >= 2:
                        records.append(StaffRecord(name=name, title=line, email=email))

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[StaffRecord] = []
    for r in records:
        key = r.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


async def scrape_school(browser: Browser, school: dict) -> tuple[list[StaffRecord], str]:
    """Scrape one school. Returns (records, status)."""
    url = school["website_url"]
    if not url:
        return [], "no_url"
    if not _can_fetch(url):
        return [], "robots_blocked"

    context = await browser.new_context(
        user_agent="Mozilla/5.0 (compatible; CityArts-TeacherFinder/1.0; +https://cityarts.org)"
    )
    page = await context.new_page()
    try:
        dir_url = await _find_directory_page(page, url)
        if not dir_url:
            return [], "no_directory_found"
        await page.goto(dir_url, timeout=20000, wait_until="networkidle")
        records = await _extract_staff(page)
        status = "ok" if records else "no_art_teachers_found"
        return records, status
    except PWTimeout:
        return [], "timeout"
    except Exception as exc:
        log.warning("  Scrape error for %s: %s", url, exc)
        return [], f"error: {exc}"
    finally:
        await context.close()


def save_staff(school_id: int, records: list[StaffRecord]) -> int:
    saved = 0
    with get_conn() as conn:
        for r in records:
            conn.execute(
                """
                INSERT OR IGNORE INTO staff (school_id, teacher_name, title, email, resolution_method)
                VALUES (?, ?, ?, ?, ?)
                """,
                (school_id, r.name, r.title, r.email, "scraped" if r.email else "unresolved"),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                saved += 1
    return saved


async def run_scraper(states: list[str], limit: int | None = None) -> None:
    with get_conn() as conn:
        q = "SELECT id, school_name, website_url FROM schools WHERE scraped=0 AND state IN ({})".format(
            ",".join("?" * len(states))
        )
        rows = conn.execute(q, states).fetchall()

    if limit:
        rows = rows[:limit]

    log.info("Scraping %d schools…", len(rows))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for i, school in enumerate(rows, 1):
            school_dict = dict(school)
            log.info("[%d/%d] %s (%s)", i, len(rows), school_dict["school_name"], school_dict["website_url"])
            records, status = await scrape_school(browser, school_dict)
            log.info("  → %d art teacher(s) found. Status: %s", len(records), status)
            saved = save_staff(school_dict["id"], records)
            with get_conn() as conn:
                conn.execute(
                    "UPDATE schools SET scraped=1, scrape_status=? WHERE id=?",
                    (status, school_dict["id"]),
                )
            log.info("  → %d new staff records saved.", saved)
            if i < len(rows):
                time.sleep(DELAY_BETWEEN_REQUESTS)
        await browser.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Scrape school staff directories.")
    parser.add_argument("--states", nargs="+", default=["TX", "KS"])
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(run_scraper([s.upper() for s in args.states], args.limit))


if __name__ == "__main__":
    main()
