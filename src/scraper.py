"""
Step 2 — Staff directory scraper using Playwright.

Usage:
    python -m src.scraper --states TX KS [--limit 10]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import time
import urllib.parse
import urllib.robotparser
from typing import NamedTuple

import aiohttp
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
    r"\b(art|fine arts?|visual arts?|studio|drawing|painting|ceramics?|sculpture|photography|printmaking|graphic design)\b",
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

# Keyword checks used while probing candidate directory/arts paths over plain HTTP
STAFF_KEYWORD_RE = re.compile(r"\b(teacher|instructor|staff|faculty)\b", re.IGNORECASE)
ARTS_KEYWORD_RE = re.compile(r"\b(art|visual arts?|fine arts?)\b", re.IGNORECASE)
CONTACT_KEYWORD_RE = re.compile(r"\b(teacher|instructor|staff|contact)\b", re.IGNORECASE)

DELAY_BETWEEN_REQUESTS = 2.5  # seconds, used when robots.txt has no Crawl-delay

# How many schools to scrape concurrently. Different schools are (almost
# always) different domains, so robots.txt crawl-delay — which is per-domain —
# doesn't require them to run one-at-a-time. Override with SCRAPE_CONCURRENCY.
DEFAULT_CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "4"))

# Timeout for the lightweight HTTP probes used to discover directory pages
PROBE_TIMEOUT = 6.0

SCRAPER_UA = "Mozilla/5.0 (compatible; CityArts-TeacherFinder/1.0; +https://cityarts.org)"


class StaffRecord(NamedTuple):
    name: str
    title: str
    email: str | None


# Splits a single "Name, Title" / "Name - Title" / "Name: Title" line in two.
# The dash variant requires surrounding spaces so it doesn't cut a hyphenated
# last name like "Smith-Jones" in half.
_NAME_TITLE_SPLIT_RE = re.compile(r"\s*,\s*|\s*:\s*|\s+[-–—]\s+")


def _looks_like_name(candidate: str) -> bool:
    """Reject obvious non-names (badges like "1 Year", "K-5", stray labels)
    that would otherwise pass a naive word-count check."""
    words = candidate.split()
    return (
        2 <= len(words) <= 5
        and len(candidate) <= 60
        and not any(ch.isdigit() for ch in candidate)
        and not _is_art_teacher(candidate)
    )


def _split_name_title(line: str) -> tuple[str, str] | None:
    """Split a single line that packs both name and title together, e.g.
    "Kayla Kinser, Art" -> ("Kayla Kinser", "Art"). Returns None if `line`
    doesn't look like a name+title pair.
    """
    parts = _NAME_TITLE_SPLIT_RE.split(line, maxsplit=1)
    if len(parts) != 2:
        return None
    name, title = parts[0].strip(), parts[1].strip()
    if not title or not _looks_like_name(name):
        return None
    return name, title


def _is_art_teacher(title: str) -> bool:
    if not title:
        return False
    if ART_FALSE_POSITIVES.search(title):
        art_match = ART_TITLE_KEYWORDS.search(title)
        if art_match and art_match.group().lower() in ("arthur", "arturo"):
            return False
    return bool(ART_TITLE_KEYWORDS.search(title))


class RobotsCache:
    """Per-domain robots.txt cache. Fetches once per domain, then serves from memory.

    Fetches are done over the shared aiohttp session so a slow/unresponsive
    robots.txt on one domain can't stall scraping of other domains running
    concurrently (a plain blocking urllib call would freeze the whole event
    loop, not just the task that's waiting on it).
    """

    def __init__(self) -> None:
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _domain(self, url: str) -> str:
        p = urllib.parse.urlparse(url)
        return f"{p.scheme}://{p.netloc}"

    async def _get(
        self, url: str, session: aiohttp.ClientSession
    ) -> urllib.robotparser.RobotFileParser | None:
        domain = self._domain(url)
        if domain in self._cache:
            return self._cache[domain]
        lock = self._locks.setdefault(domain, asyncio.Lock())
        async with lock:
            if domain in self._cache:  # someone else fetched it while we waited
                return self._cache[domain]
            # None means "no usable robots.txt" -> can_fetch()/crawl_delay() both
            # treat that as allow-all. A freshly constructed but never-.parse()d
            # RobotFileParser is NOT equivalent: its can_fetch() defaults to
            # False (deny-all), so leaving it uncached here would silently
            # block every site whose robots.txt 404s, times out, or fails to
            # decode (e.g. aiohttp lacking a brotli decoder) even though the
            # intent below is to allow those.
            rp: urllib.robotparser.RobotFileParser | None = None
            robots_url = f"{domain}/robots.txt"
            try:
                async with session.get(
                    robots_url, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text(errors="replace")
                        rp = urllib.robotparser.RobotFileParser()
                        rp.parse(text.splitlines())
                    # non-200 (404, 403, ...) → no usable robots.txt → allow all
            except Exception:
                pass  # network error, timeout, decode error → allow all
            self._cache[domain] = rp
        return self._cache[domain]

    async def can_fetch(self, url: str, session: aiohttp.ClientSession) -> bool:
        rp = await self._get(url, session)
        if rp is None:
            return True
        return rp.can_fetch(SCRAPER_UA, url)

    async def crawl_delay(self, url: str, session: aiohttp.ClientSession) -> float:
        rp = await self._get(url, session)
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


async def _probe_paths(
    session: aiohttp.ClientSession,
    base_url: str,
    paths: list[str],
    match_fn,
    robots: RobotsCache,
) -> str | None:
    """Concurrently GET every candidate path and return the first (by priority
    order in `paths`, not order of completion) whose body satisfies match_fn.

    This replaces what used to be up to a dozen-plus sequential full-browser
    Playwright navigations with one round of plain HTTP requests, which is
    where nearly all of the scrape time per school was going.
    """
    async def check(path: str) -> str | None:
        candidate = base_url.rstrip("/") + path
        if not await robots.can_fetch(candidate, session):
            return None
        try:
            async with session.get(
                candidate, timeout=aiohttp.ClientTimeout(total=PROBE_TIMEOUT), allow_redirects=True
            ) as resp:
                if resp.status >= 400:
                    return None
                text = await resp.text(errors="replace")
                if match_fn(text):
                    return candidate
        except Exception:
            return None
        return None

    results = await asyncio.gather(*(check(p) for p in paths))
    return next((r for r in results if r), None)


async def _find_directory_page(
    page: Page,
    base_url: str,
    session: aiohttp.ClientSession,
    robots: RobotsCache,
    cached_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Try known paths (concurrently, over plain HTTP) and look for
    staff-related links on the homepage as a last resort.

    `cached_path` is a relative path (e.g. "/staff") that already worked for
    another school in the same district — districts overwhelmingly run every
    school's site on the same CMS/template, so it's tried first, alone,
    before the full candidate list.

    Returns (found_url, matched_relative_path). matched_relative_path is only
    set when the hit came from a known template path (cached or from
    DIRECTORY_PATHS) — i.e. something reusable for sibling schools in the
    same district — and is None when found via the homepage link scan
    (site-specific, not a generic template path).
    """
    match_fn = lambda text: bool(EMAIL_RE.search(text) or STAFF_KEYWORD_RE.search(text))

    if cached_path:
        hit = await _probe_paths(session, base_url, [cached_path], match_fn, robots)
        if hit:
            log.info("  Found directory at %s (district cache hit: %s)", hit, cached_path)
            return hit, cached_path

    hit = await _probe_paths(session, base_url, DIRECTORY_PATHS, match_fn, robots)
    if hit:
        log.info("  Found directory at %s", hit)
        return hit, hit[len(base_url.rstrip("/")):]

    # Fall back: look for links on homepage (needs a real browser — some nav
    # menus are JS-rendered — but this is a single navigation, not a loop).
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
                return full, None
    except Exception as exc:
        log.debug("  Homepage link scan failed: %s", exc)

    return None, None


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
        if not _looks_like_name(name):
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
            if not lines:
                continue
            # A "Name, Title" pair can appear packed on any line of the block,
            # not necessarily the first (e.g. a "1 Year" tenure badge before it).
            name_candidate = title_candidate = None
            for line in lines:
                split = _split_name_title(line)
                if split and _is_art_teacher(split[1]):
                    name_candidate, title_candidate = split
                    break
            if name_candidate is None:
                if len(lines) < 2:
                    continue
                name_candidate = lines[0]
                title_candidate = _TITLE_CLEANUP_RE.sub("", " ".join(lines[1:3])).strip()
                if not _is_art_teacher(title_candidate):
                    continue
            if not _looks_like_name(name_candidate):
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
                split = _split_name_title(line)
                if split and _is_art_teacher(split[1]):
                    records.append(StaffRecord(name=split[0], title=split[1], email=email))
                    continue
                if _is_art_teacher(line):
                    name = lines[i - 1] if i > 0 else ""
                    if _looks_like_name(name):
                        records.append(StaffRecord(name=name, title=line, email=email))

    # Strategy 4: Plain-text line scan — art title line, name on a preceding line.
    if not records:
        all_lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(all_lines):
            split = _split_name_title(line)
            if split and _is_art_teacher(split[1]):
                name, title = split
                context_block = " ".join(all_lines[max(0, i - 1):i + 2])
                email_matches = EMAIL_RE.findall(context_block)
                records.append(StaffRecord(
                    name=name,
                    title=title,
                    email=email_matches[0] if email_matches else None,
                ))
                continue
            if _is_art_teacher(line):
                for j in range(i - 1, max(i - 4, -1), -1):
                    candidate = all_lines[j]
                    if _looks_like_name(candidate):
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


async def _find_arts_page(
    page: Page,
    base_url: str,
    session: aiohttp.ClientSession,
    robots: RobotsCache,
    cached_path: str | None = None,
) -> tuple[str | None, str | None]:
    """Search for a district-level fine arts / visual arts department page.

    Same district-cache/return-shape contract as `_find_directory_page`.
    """
    match_fn = lambda text: bool(ARTS_KEYWORD_RE.search(text) and (
        EMAIL_RE.search(text) or CONTACT_KEYWORD_RE.search(text)
    ))

    if cached_path:
        hit = await _probe_paths(session, base_url, [cached_path], match_fn, robots)
        if hit:
            log.info("  Found arts dept page at %s (district cache hit: %s)", hit, cached_path)
            return hit, cached_path

    hit = await _probe_paths(session, base_url, ARTS_PATHS, match_fn, robots)
    if hit:
        log.info("  Found arts dept page at %s", hit)
        return hit, hit[len(base_url.rstrip("/")):]

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
                return full, None
    except Exception as exc:
        log.debug("  Arts nav scan failed: %s", exc)

    return None, None


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
            if _looks_like_name(line):
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
            # A "Name, Title" pair can appear packed on any line of the block,
            # not necessarily the first (e.g. a "1 Year" tenure badge before it).
            name_candidate = title = None
            for line in lines:
                split = _split_name_title(line)
                if split:
                    name_candidate, title = split
                    break
            if name_candidate is None:
                name_candidate = lines[0]
                title = _TITLE_CLEANUP_RE.sub("", lines[1]).strip() if len(lines) > 1 else "Visual Arts"
            if "@" in name_candidate or not _looks_like_name(name_candidate):
                continue
            email = item_emails[0] if item_emails else None
            if not email:
                found = EMAIL_RE.findall(raw)
                email = found[0] if found else None
            if not email and msg_link:
                email = msg_link
            if not email:
                continue  # require contact info in permissive mode
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
    browser: Browser,
    school: dict,
    robots: RobotsCache | None = None,
    session: aiohttp.ClientSession | None = None,
    district_dir_cache: dict[str, str] | None = None,
    district_arts_cache: dict[str, str] | None = None,
) -> tuple[list[StaffRecord], str]:
    """Scrape one school. Returns (records, status).

    district_dir_cache / district_arts_cache map district_name -> a relative
    path (e.g. "/staff") that already worked for a sibling school in that
    district. Districts overwhelmingly run every school's site on the same
    CMS/template, so once the pattern is learned from the first school, later
    schools in the same district try that one path first instead of the full
    candidate list.
    """
    url = school["website_url"]
    if not url:
        return [], "no_url"
    if robots is None:
        robots = RobotsCache()

    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers={"User-Agent": SCRAPER_UA})

    if not await robots.can_fetch(url, session):
        if own_session:
            await session.close()
        return [], "robots_blocked"

    district = school.get("district_name")
    cached_dir_path = district_dir_cache.get(district) if (district_dir_cache and district) else None
    cached_arts_path = district_arts_cache.get(district) if (district_arts_cache and district) else None

    context = await browser.new_context(user_agent=SCRAPER_UA)
    page = await context.new_page()
    try:
        dir_url, dir_path = await _find_directory_page(page, url, session, robots, cached_dir_path)
        arts_page = False
        if not dir_url:
            dir_url, arts_path = await _find_arts_page(page, url, session, robots, cached_arts_path)
            if not dir_url:
                return [], "no_directory_found"
            arts_page = True
            if arts_path and district_arts_cache is not None and district:
                district_arts_cache[district] = arts_path
        elif dir_path and district_dir_cache is not None and district:
            district_dir_cache[district] = dir_path
        await page.goto(dir_url, timeout=20000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)
        records = await (_extract_staff_permissive(page) if arts_page else _extract_staff(page))
        # Even on a normal staff directory, fall back to arts page if we found no one
        if not records and not arts_page:
            arts_url, arts_path = await _find_arts_page(page, url, session, robots, cached_arts_path)
            if arts_url:
                if arts_path and district_arts_cache is not None and district:
                    district_arts_cache[district] = arts_path
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
        if own_session:
            await session.close()


def save_staff(school_id: int, records: list[StaffRecord]) -> int:
    import datetime
    saved = 0
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_conn() as conn:
        for r in records:
            conn.execute(
                """
                INSERT OR IGNORE INTO staff (school_id, teacher_name, title, email, resolution_method, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (school_id, r.name, r.title, r.email, "scraped" if r.email else "unresolved", now),
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
    should_stop=None,
    concurrency: int | None = None,
) -> None:
    """Run the scraper.

    Schools are scraped concurrently (bounded by `concurrency`, default
    SCRAPE_CONCURRENCY env var or 4). Since each school is almost always a
    different domain, and robots.txt crawl-delay is a per-domain rule, this
    doesn't violate crawl-delay — schools on the same domain are still
    spaced out by that domain's delay, just not schools on different domains.

    on_progress(current, total, school_name, status) is called once a school
    starts and again once it finishes, so callers can track progress in real
    time. Because schools run concurrently, calls may arrive out of the
    original row order.

    should_stop() is polled before each school starts; if it returns True,
    schools not yet started are skipped (in-flight ones finish normally).
    """
    with get_conn() as conn:
        state_ph_check = ",".join("?" * len(states))
        ingested_states = {
            row["state"]
            for row in conn.execute(
                f"SELECT DISTINCT state FROM schools WHERE state IN ({state_ph_check})",
                states,
            ).fetchall()
        }
    missing_states = [s for s in states if s not in ingested_states]
    if missing_states:
        from .ingest import ingest_schools
        log.info("No schools ingested yet for %s — ingesting from NCES data now.", missing_states)
        ingest_schools(missing_states)

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
        q = """SELECT id, school_name, website_url, district_name FROM schools
               WHERE scraped=0 AND state IN ({})
               ORDER BY is_arts_school DESC, school_name""".format(
            ",".join("?" * len(states))
        )
        rows = conn.execute(q, states).fetchall()

    if limit:
        rows = rows[:limit]

    total = len(rows)
    if concurrency is None:
        concurrency = DEFAULT_CONCURRENCY
    log.info("Scraping %d schools… (concurrency=%d)", total, concurrency)
    robots = RobotsCache()

    completed = 0
    domain_locks: dict[str, asyncio.Lock] = {}
    domain_last_start: dict[str, float] = {}
    # Shared across all schools in this run: once a school's directory/arts
    # path is discovered via a known template path, siblings in the same
    # district try that exact path first (see scrape_school's docstring).
    district_dir_cache: dict[str, str] = {}
    district_arts_cache: dict[str, str] = {}

    def _domain(url: str) -> str:
        return urllib.parse.urlparse(url).netloc.lower()

    async def _throttle(url: str) -> None:
        """Enforce this domain's crawl-delay against its own last request,
        without blocking requests to other domains."""
        domain = _domain(url)
        lock = domain_locks.setdefault(domain, asyncio.Lock())
        async with lock:
            delay = await robots.crawl_delay(url, session)
            last = domain_last_start.get(domain)
            if last is not None:
                wait = delay - (time.monotonic() - last)
                if wait > 0:
                    await asyncio.sleep(wait)
            domain_last_start[domain] = time.monotonic()

    sem = asyncio.Semaphore(concurrency)

    async with aiohttp.ClientSession(headers={"User-Agent": SCRAPER_UA}) as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)

            async def worker(school_row) -> None:
                nonlocal completed
                school_dict = dict(school_row)
                async with sem:
                    if should_stop and should_stop():
                        return
                    if school_dict["website_url"]:
                        await _throttle(school_dict["website_url"])
                    log.info("[%d/%d] %s (%s)", completed + 1, total,
                             school_dict["school_name"], school_dict["website_url"])
                    if on_progress:
                        on_progress(completed + 1, total, school_dict["school_name"], "scraping")
                    records, status = await scrape_school(
                        browser, school_dict, robots, session,
                        district_dir_cache, district_arts_cache,
                    )
                    log.info("  → %d art teacher(s) found. Status: %s", len(records), status)
                    saved = save_staff(school_dict["id"], records)
                    with get_conn() as conn:
                        conn.execute(
                            "UPDATE schools SET scraped=1, scrape_status=? WHERE id=?",
                            (status, school_dict["id"]),
                        )
                    log.info("  → %d new staff records saved.", saved)
                    completed += 1
                    if on_progress:
                        on_progress(completed, total, school_dict["school_name"], status)

            await asyncio.gather(*(worker(row) for row in rows))
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
