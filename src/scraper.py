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

# Paths to try when hunting for a district-level fine arts / visual arts department page
ARTS_PATHS = [
    "/fine-arts",
    "/visual-arts",
    "/programs/fine-arts",
    "/programs/visual-arts",
    "/arts",
    "/departments/fine-arts",
    "/departments/visual-arts",
    "/curriculum/fine-arts",
    "/fine-arts-department",
    "/visual-arts-department",
    "/domain/fine-arts",       # Schoolwires/Blackboard CMS
    "/domain/visual-arts",
    "/domain/fine_arts",
    "/domain/visual_arts",
    "/page/fine-arts",
    "/page/visual-arts",
    "/fine_arts",
    "/visual_arts",
]

# Art-related keywords in job title (case-insensitive)
ART_TITLE_KEYWORDS = re.compile(
    r"\b(art|visual arts?|studio|drawing|painting|ceramics?|sculpture|photography|printmaking|graphic design)\b",
    re.IGNORECASE,
)

# Exclude "art" that appears to be part of a proper name (e.g. "Arthur")
ART_FALSE_POSITIVES = re.compile(r"\b(arthur|arturo|eart|mcart|smart)\b", re.IGNORECASE)

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Text patterns that indicate a "send a message" contact form link
SEND_MESSAGE_RE = re.compile(
    r"\b(send\s+a?\s*message|contact\s+me|email\s+me|message\s+me|send\s+email)\b",
    re.IGNORECASE,
)

# Strip these button labels from scraped title text
_TITLE_CLEANUP_RE = re.compile(r"\s*\bsend\s+(?:a\s+)?message\b.*$", re.IGNORECASE)

# Link-text keywords that suggest a page is a staff/people directory
DIRECTORY_LINK_RE = re.compile(
    r"\b(staff|faculty|directory|teachers?|contact|personnel|employees?|team|people|campus\s+contacts?|our\s+staff|meet\s+(our\s+)?(staff|team|teachers?))\b",
    re.IGNORECASE,
)

DELAY_BETWEEN_REQUESTS = 2.5  # seconds, used when robots.txt has no Crawl-delay

SCRAPER_UA = "Mozilla/5.0 (compatible; CityArts-TeacherFinder/1.0; +https://cityarts.org)"


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


class RobotsCache:
    """Per-domain robots.txt cache. Fetches once per domain, then serves from memory."""

    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}

    def _domain(self, url: str) -> str:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    def _get(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        domain = self._domain(url)
        if domain not in self._cache:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{domain}/robots.txt"
            try:
                req = urllib.request.Request(
                    robots_url, headers={"User-Agent": SCRAPER_UA}
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    if resp.status == 200:
                        rp.parse(resp.read().decode("utf-8", errors="replace").splitlines())
                    # 404 → no robots.txt → allow all (leave rp empty)
            except Exception:
                pass  # network error or non-200 → allow all
            self._cache[domain] = rp
        return self._cache[domain]

    def can_fetch(self, url: str) -> bool:
        rp = self._get(url)
        if rp is None:
            return True
        return rp.can_fetch(SCRAPER_UA, url)

    def crawl_delay(self, url: str) -> float:
        rp = self._get(url)
        if rp is None:
            return DELAY_BETWEEN_REQUESTS
        delay = rp.crawl_delay(SCRAPER_UA) or rp.crawl_delay("*")
        return float(delay) if delay else DELAY_BETWEEN_REQUESTS


def _same_domain(href: str, base_url: str) -> bool:
    """Return True if href is relative or shares the same hostname as base_url."""
    if href.startswith("/"):
        return True
    try:
        h = urllib.parse.urlparse(href).netloc.lstrip("www.")
        b = urllib.parse.urlparse(base_url).netloc.lstrip("www.")
        return bool(h) and h == b
    except Exception:
        return False


def _resolve_href(href: str, base_url: str) -> str:
    """Turn a relative or same-domain href into an absolute URL."""
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + ("" if href.startswith("/") else "/") + href


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
            text = link.get("text", "").strip()
            if DIRECTORY_LINK_RE.search(text) and _same_domain(href, base_url):
                full = _resolve_href(href, base_url)
                log.info("  Found possible directory link: %s (%r)", full, text)
                return full
    except Exception as exc:
        log.debug("  Homepage link scan failed: %s", exc)

    return None


async def _extract_staff(page: Page) -> list[StaffRecord]:
    """Extract staff names, titles and emails from the current page."""
    text = await page.inner_text("body")
    records: list[StaffRecord] = []

    # Strategy 1: Semantic name/title elements (Apptegy, Finalsite, similar CMSes).
    # Looks for any element whose class contains "name" paired with a sibling "title".
    semantic = await page.eval_on_selector_all(
        "[class*='name']",
        """els => els.flatMap(nameEl => {
            const parent = nameEl.parentElement;
            if (!parent) return [];
            const titleEl = parent.querySelector('[class*=\"title\"]');
            if (!titleEl) return [];
            const card = parent.closest('[class*=\"staff\"],[class*=\"person\"],[class*=\"card\"]') || parent;
            const emailA = card.querySelector('a[href^=\"mailto:\"]');
            // Detect "send a message" / contact form links when no mailto found
            let contactHref = '';
            if (!emailA) {
                const allLinks = [...card.querySelectorAll('a[href]')];
                const msgLink = allLinks.find(a =>
                    /send\\s+a?\\s*message|contact\\s+me|email\\s+me|message\\s+me/i.test(a.textContent)
                );
                if (msgLink) contactHref = msgLink.href;
            }
            return [{
                name: nameEl.innerText.trim(),
                title: titleEl.innerText.trim(),
                email: emailA ? emailA.href.replace('mailto:','') : contactHref
            }];
        })""",
    )
    for item in semantic:
        name = item.get("name", "").strip()
        title = _TITLE_CLEANUP_RE.sub("", item.get("title", "")).strip()
        if not _is_art_teacher(title):
            continue
        if len(name.split()) < 2 or len(name) > 60:
            continue
        records.append(StaffRecord(name=name, title=title, email=item.get("email") or None))

    # Strategy 2: Generic card/row blobs — grab innerText and parse by line position.
    if not records:
        candidates = await page.eval_on_selector_all(
            "tr, li, .staff-member, .faculty-member, [class*='staff'], [class*='faculty'], [class*='employee'], [class*='person'], [class*='card']",
            """els => els.map(el => ({
                text: el.innerText,
                emails: [...el.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:','')),
                msgLink: (() => {
                    const lnk = [...el.querySelectorAll('a[href]')].find(a =>
                        /send\\s+a?\\s*message|contact\\s+me|email\\s+me|message\\s+me/i.test(a.textContent)
                    );
                    return lnk ? lnk.href : '';
                })()
            }))""",
        )
        for item in candidates:
            raw = item.get("text", "").strip()
            item_emails = item.get("emails", [])
            msg_link = item.get("msgLink", "")
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if len(lines) < 2:
                continue
            name_candidate = lines[0]
            title_candidate = _TITLE_CLEANUP_RE.sub("", " ".join(lines[1:3])).strip()
            if not _is_art_teacher(title_candidate):
                continue
            if len(name_candidate.split()) < 2 or len(name_candidate) > 60:
                continue
            email = item_emails[0] if item_emails else None
            if not email:
                block_emails = EMAIL_RE.findall(raw)
                email = block_emails[0] if block_emails else None
            if not email and msg_link:
                email = msg_link
            records.append(StaffRecord(name=name_candidate, title=title_candidate, email=email))

    # Strategy 3: mailto links — use surrounding container text to find name/title.
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

    # Strategy 4: Plain-text line scan — art title line, name on a preceding line.
    if not records:
        all_lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(all_lines):
            if _is_art_teacher(line):
                for j in range(i - 1, max(i - 4, -1), -1):
                    candidate = all_lines[j]
                    words = candidate.split()
                    if 2 <= len(words) <= 5 and len(candidate) <= 50 and not _is_art_teacher(candidate):
                        context_block = " ".join(all_lines[max(0, i - 2):i + 3])
                        email_matches = EMAIL_RE.findall(context_block)
                        records.append(StaffRecord(
                            name=candidate,
                            title=line,
                            email=email_matches[0] if email_matches else None,
                        ))
                        break

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[StaffRecord] = []
    for r in records:
        key = r.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


async def _find_arts_page(page: Page, base_url: str) -> str | None:
    """Search for a district-level fine arts / visual arts department page."""
    for path in ARTS_PATHS:
        candidate = base_url.rstrip("/") + path
        try:
            resp = await page.goto(candidate, timeout=12000, wait_until="domcontentloaded")
            await asyncio.sleep(0.5)
            if resp and resp.status < 400:
                text = await page.inner_text("body")
                if re.search(r"\b(art|visual arts?|fine arts?)\b", text, re.I) and (
                    EMAIL_RE.search(text) or re.search(r"\b(teacher|instructor|staff|contact)\b", text, re.I)
                ):
                    log.info("  Found arts dept page at %s", candidate)
                    return candidate
        except PWTimeout:
            pass
        except Exception as exc:
            log.debug("  Arts path %s: %s", path, exc)
        await asyncio.sleep(0.3)

    # Scan homepage navigation for fine arts / visual arts links
    try:
        await page.goto(base_url, timeout=15000, wait_until="domcontentloaded")
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({href: e.href, text: e.textContent}))",
        )
        for link in links:
            href = link.get("href", "")
            text = link.get("text", "").strip()
            if re.search(r"\b(fine arts?|visual arts?)\b", text, re.I) and _same_domain(href, base_url):
                full = _resolve_href(href, base_url)
                log.info("  Found arts nav link: %s (%r)", full, text)
                return full
    except Exception as exc:
        log.debug("  Arts nav scan failed: %s", exc)

    return None


async def _extract_staff_permissive(page: Page) -> list[StaffRecord]:
    """Extract staff from an arts-specific page — no art-title keyword required.

    Used when the page context (fine arts / visual arts dept) already implies
    everyone listed is an art teacher.
    """
    records: list[StaffRecord] = []

    # Strategy 1: All mailto links — find name in surrounding container text.
    mailto_links = await page.eval_on_selector_all(
        "a[href^='mailto:']",
        """els => els.map(el => ({
            email: el.href.replace('mailto:', ''),
            text: (el.closest('tr,li,div,p,td') || el.parentElement).innerText
        }))""",
    )
    for link in mailto_links:
        email = link.get("email", "")
        surrounding = link.get("text", "")
        lines = [l.strip() for l in surrounding.splitlines() if l.strip()]
        for line in lines:
            if "@" in line:
                continue
            words = line.split()
            if 2 <= len(words) <= 5 and len(line) <= 50:
                records.append(StaffRecord(name=line, title="Visual Arts", email=email))
                break

    # Strategy 2: Cards / rows that have a name + email (email required).
    if not records:
        candidates = await page.eval_on_selector_all(
            "tr, li, [class*='staff'], [class*='person'], [class*='card'], [class*='teacher'], [class*='faculty']",
            """els => els.map(el => ({
                text: el.innerText,
                emails: [...el.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:', '')),
                msgLink: (() => {
                    const lnk = [...el.querySelectorAll('a[href]')].find(a =>
                        /send\\s+a?\\s*message|contact\\s+me|email\\s+me|message\\s+me/i.test(a.textContent)
                    );
                    return lnk ? lnk.href : '';
                })()
            }))""",
        )
        for item in candidates:
            raw = item.get("text", "").strip()
            item_emails = item.get("emails", [])
            msg_link = item.get("msgLink", "")
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if not lines:
                continue
            name_candidate = lines[0]
            words = name_candidate.split()
            if len(words) < 2 or len(name_candidate) > 60 or "@" in name_candidate:
                continue
            email = item_emails[0] if item_emails else None
            if not email:
                found = EMAIL_RE.findall(raw)
                email = found[0] if found else None
            if not email and msg_link:
                email = msg_link
            if not email:
                continue  # require contact info in permissive mode
            title = _TITLE_CLEANUP_RE.sub("", lines[1]).strip() if len(lines) > 1 else "Visual Arts"
            records.append(StaffRecord(name=name_candidate, title=title or "Visual Arts", email=email))

    # Deduplicate by name
    seen: set[str] = set()
    unique: list[StaffRecord] = []
    for r in records:
        key = r.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


async def scrape_school(
    browser: Browser, school: dict, robots: RobotsCache | None = None
) -> tuple[list[StaffRecord], str]:
    """Scrape one school. Returns (records, status)."""
    url = school["website_url"]
    if not url:
        return [], "no_url"
    if robots is None:
        robots = RobotsCache()
    if not robots.can_fetch(url):
        return [], "robots_blocked"

    context = await browser.new_context(user_agent=SCRAPER_UA)
    page = await context.new_page()
    try:
        dir_url = await _find_directory_page(page, url)
        arts_page = False
        if not dir_url:
            dir_url = await _find_arts_page(page, url)
            if not dir_url:
                return [], "no_directory_found"
            arts_page = True
        await page.goto(dir_url, timeout=20000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        records = await (_extract_staff_permissive(page) if arts_page else _extract_staff(page))
        # Even on a normal staff directory, fall back to arts page if we found no one
        if not records and not arts_page:
            arts_url = await _find_arts_page(page, url)
            if arts_url:
                await page.goto(arts_url, timeout=20000, wait_until="domcontentloaded")
                await asyncio.sleep(1.5)
                records = await _extract_staff_permissive(page)
                if records:
                    dir_url = arts_url

        # Follow pagination: detect the param name and max page, then hit every page.
        _PAGINATION_RE = re.compile(r"[?&](page_no|page|p)=(\d+)", re.IGNORECASE)
        all_page_links = await page.eval_on_selector_all(
            "a[href]", "els => els.map(e => e.href)"
        )
        param_name: str | None = None
        max_page = 1
        base_paginated = dir_url
        for href in all_page_links:
            m = _PAGINATION_RE.search(href)
            if m:
                pn, num = m.group(1), int(m.group(2))
                if num > max_page:
                    max_page = num
                    param_name = pn
                    base_paginated = re.sub(r"[?&]" + pn + r"=\d+", "", href).rstrip("?&")
        if param_name and max_page > 1:
            sep = "&" if "?" in base_paginated else "?"
            for pnum in range(2, max_page + 1):
                href = f"{base_paginated}{sep}{param_name}={pnum}"
                try:
                    await page.goto(href, timeout=20000, wait_until="domcontentloaded")
                    await asyncio.sleep(1.0)
                    records.extend(await _extract_staff(page))
                except Exception as exc:
                    log.debug("  Pagination page %s failed: %s", href, exc)

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


# Statuses that indicate the school was skipped/missed rather than truly scraped
MISSED_STATUSES = ("no_directory_found", "timeout")


async def run_scraper(
    states: list[str],
    limit: int | None = None,
    rescrape_missed: bool = False,
    on_progress=None,
) -> None:
    """Run the scraper.

    on_progress(current, total, school_name, status) is called before and after
    each school so callers can track progress in real time.
    """
    with get_conn() as conn:
        if rescrape_missed:
            state_ph = ",".join("?" * len(states))
            n = conn.execute(
                f"""UPDATE schools SET scraped=0
                    WHERE state IN ({state_ph})
                      AND (scrape_status IN ({','.join('?' * len(MISSED_STATUSES))})
                           OR scrape_status LIKE 'error:%')""",
                list(states) + list(MISSED_STATUSES),
            ).rowcount
            log.info("Reset %d missed school(s) for re-scraping.", n)
        q = "SELECT id, school_name, website_url FROM schools WHERE scraped=0 AND state IN ({})".format(
            ",".join("?" * len(states))
        )
        rows = conn.execute(q, states).fetchall()

    if limit:
        rows = rows[:limit]

    total = len(rows)
    log.info("Scraping %d schools…", total)
    robots = RobotsCache()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        for i, school in enumerate(rows, 1):
            school_dict = dict(school)
            log.info("[%d/%d] %s (%s)", i, total, school_dict["school_name"], school_dict["website_url"])
            if on_progress:
                on_progress(i, total, school_dict["school_name"], "scraping")
            records, status = await scrape_school(browser, school_dict, robots)
            log.info("  → %d art teacher(s) found. Status: %s", len(records), status)
            saved = save_staff(school_dict["id"], records)
            with get_conn() as conn:
                conn.execute(
                    "UPDATE schools SET scraped=1, scrape_status=? WHERE id=?",
                    (status, school_dict["id"]),
                )
            log.info("  → %d new staff records saved.", saved)
            if on_progress:
                on_progress(i, total, school_dict["school_name"], status)
            if i < total:
                delay = robots.crawl_delay(school_dict["website_url"]) if school_dict["website_url"] else DELAY_BETWEEN_REQUESTS
                time.sleep(delay)
        await browser.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Scrape school staff directories.")
    parser.add_argument("--states", nargs="+", default=["TX", "KS"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--rescrape",
        action="store_true",
        help="Reset missed schools (no_directory_found, timeout, error:*) and scrape everything.",
    )
    args = parser.parse_args()
    asyncio.run(run_scraper([s.upper() for s in args.states], args.limit, args.rescrape))


if __name__ == "__main__":
    main()
