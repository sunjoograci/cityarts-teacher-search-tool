"""
Staff directory scraper — orchestrates Stage 1-5 (entity resolution, path
discovery, access-barrier handling, email decoding, art classification) into
one per-school crawl, then persists Stage 6's output contract.

Usage:
    python -m src.scraper --states TX KS [--limit 10]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass
from typing import Optional

import aiohttp
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout

from . import access_strategies as acc
from . import art_classifier
from . import email_decoder as ed
from . import entity_resolver as er
from . import path_discovery as pd
from .db import get_conn

log = logging.getLogger(__name__)

<<<<<<< HEAD
EMAIL_RE = ed.EMAIL_RE
=======
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
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb

# Text patterns that indicate a "send a message" contact form link
SEND_MESSAGE_RE = re.compile(
    r"\b(send\s+a?\s*message|contact\s+me|email\s+me|message\s+me|send\s+email)\b",
    re.IGNORECASE,
)

# Strip these button labels from scraped title text
_TITLE_CLEANUP_RE = re.compile(r"\s*\bsend\s+(?:a\s+)?message\b.*$", re.IGNORECASE)

STAFF_KEYWORD_RE = re.compile(r"\b(teacher|instructor|staff|faculty)\b", re.IGNORECASE)
ARTS_KEYWORD_RE = re.compile(r"\b(art|visual arts?|fine arts?)\b", re.IGNORECASE)

DELAY_BETWEEN_REQUESTS = 2.5  # seconds, used when robots.txt has no Crawl-delay

DEFAULT_CONCURRENCY = int(os.environ.get("SCRAPE_CONCURRENCY", "4"))
PROBE_TIMEOUT = 6.0
SCRAPER_UA = "Mozilla/5.0 (compatible; CityArts-TeacherFinder/1.0; +https://cityarts.org)"

# Statuses matching the output contract (Stage 6).
STATUS_OK = "OK"
STATUS_NO_TEACHERS_LISTED = "NO_TEACHERS_LISTED"
STATUS_NO_DIRECTORY_FOUND = "NO_DIRECTORY_FOUND"
STATUS_AUTH_REQUIRED = "AUTH_REQUIRED"
STATUS_AMBIGUOUS_ENTITY = "AMBIGUOUS_ENTITY"
STATUS_NOT_A_SCHOOL = "NOT_A_SCHOOL"
STATUS_PROGRAM_REDIRECTED = "PROGRAM_REDIRECTED"
STATUS_BLOCKED = "BLOCKED"
STATUS_TIMEOUT = "TIMEOUT"
STATUS_ERROR_PREFIX = "error:"

# Statuses that indicate the school was skipped/missed rather than truly scraped
MISSED_STATUSES = (STATUS_NO_DIRECTORY_FOUND, STATUS_TIMEOUT, STATUS_BLOCKED)


@dataclass
class StaffRecord:
    name: str
    title: str
<<<<<<< HEAD
    email: Optional[str] = None
    discipline: str = "unknown"
    email_source: Optional[str] = None
    email_verified: bool = False
    evidence_url: Optional[str] = None
=======
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
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb


class RobotsCache:
    """Per-domain robots.txt cache. Fetches once per domain, then serves from memory."""

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
            if domain in self._cache:
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
<<<<<<< HEAD
                        rp.parse(text.splitlines())  # parse() calls self.modified(), setting last_checked
                    elif resp.status in (401, 403):
                        # Mirrors stdlib RobotFileParser.read(): explicit auth
                        # failure on robots.txt itself -> treat as disallow-all.
                        rp.disallow_all = True
                        rp.modified()
                    elif 400 <= resp.status < 500:
                        # No robots.txt at this path (404 etc.) -> allow all.
                        # This is the single most common case on the open web
                        # (most small sites don't publish one) and MUST set
                        # last_checked, or can_fetch()'s "not self.last_checked
                        # -> return False" guard silently blocks everything.
                        rp.allow_all = True
                        rp.modified()
                    else:
                        # 5xx or unexpected status: transient server issue, not
                        # a real robots directive — don't penalize the crawl for it.
                        rp.allow_all = True
                        rp.modified()
            except Exception:
                # Network/timeout error reaching robots.txt — same reasoning:
                # unknown is not the same as disallowed.
                rp.allow_all = True
                rp.modified()
=======
                        rp = urllib.robotparser.RobotFileParser()
                        rp.parse(text.splitlines())
                    # non-200 (404, 403, ...) → no usable robots.txt → allow all
            except Exception:
                pass  # network error, timeout, decode error → allow all
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb
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


# ---------------------------------------------------------------------------
# Stage 2/3: directory discovery + access-barrier handling
# ---------------------------------------------------------------------------

async def find_directory(
    page: Page,
    base_url: str,
    session: aiohttp.ClientSession,
    robots: RobotsCache,
    cached_candidate: Optional[pd.PathCandidate] = None,
) -> tuple[Optional[str], list[dict], Optional[str]]:
    """Run Stage 2 discovery + probing. Returns (directory_url, paths_attempted
    as plain dicts, matched_source). `paths_attempted` is always populated —
    a caller may only emit NO_DIRECTORY_FOUND once this covers all sources.
    """
    match_fn = lambda text: bool(EMAIL_RE.search(text) or STAFF_KEYWORD_RE.search(text))
    attempts: list[pd.PathAttempt] = []

    if cached_candidate:
        hit, cached_attempts = await pd.probe_candidates(session, [cached_candidate], robots, match_fn)
        attempts.extend(cached_attempts)
        if hit:
            return hit.url, [a.__dict__ for a in attempts], hit.source

    discovery = await pd.discover(base_url, session, robots)
    attempts.extend(discovery.attempts)
    hit, probe_attempts = await pd.probe_candidates(session, discovery.candidates, robots, match_fn)
    attempts.extend(probe_attempts)
    if hit:
        return hit.url, [a.__dict__ for a in attempts], hit.source

    return None, [a.__dict__ for a in attempts], None


# ---------------------------------------------------------------------------
# Stage 4/5: extraction — email decoding + art classification
# ---------------------------------------------------------------------------

async def _extract_staff(page: Page, evidence_url: str, permissive: bool = False) -> list[StaffRecord]:
    """Extract staff records from the current page.

    permissive=True is used on an arts-department page, where everyone
    listed is already known to be art-adjacent, so the art_classifier gate
    is skipped (discipline still gets tagged, just without requiring a
    title-keyword match first).
    """
    records: list[StaffRecord] = []

    # Strategy 1: Semantic name/title cards (Apptegy, Finalsite, similar CMSes).
    semantic = await page.eval_on_selector_all(
        "[class*='name']",
        """els => els.flatMap(nameEl => {
            const parent = nameEl.parentElement;
            if (!parent) return [];
            const titleEl = parent.querySelector('[class*="title"]');
            if (!titleEl) return [];
            const card = parent.closest('[class*="staff"],[class*="person"],[class*="card"]') || parent;
            const emailA = card.querySelector('a[href^="mailto:"]');
            const cfSpan = card.querySelector('[data-cfemail]');
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
                email: emailA ? emailA.href.replace('mailto:','') : contactHref,
                cfemail: cfSpan ? cfSpan.getAttribute('data-cfemail') : '',
                rawText: card.innerText,
            }];
        })""",
    )
    for item in semantic:
        name = item.get("name", "").strip()
        title = _TITLE_CLEANUP_RE.sub("", item.get("title", "")).strip()
        classification = art_classifier.classify(title)
        if not permissive and not classification.is_art:
            continue
        if not _looks_like_name(name):
            continue
        email, source = _resolve_email(item.get("email") or "", item.get("cfemail") or "", item.get("rawText") or "")
        records.append(StaffRecord(
            name=name, title=title, email=email,
            discipline=classification.discipline, email_source=source,
            evidence_url=evidence_url,
        ))

    # Strategy 2: Generic card/row blobs.
    if not records:
        candidates = await page.eval_on_selector_all(
            "tr, li, .staff-member, .faculty-member, [class*='staff'], [class*='faculty'], [class*='employee'], [class*='person'], [class*='card']",
            """els => els.map(el => ({
                text: el.innerText,
                emails: [...el.querySelectorAll('a[href^="mailto:"]')].map(a => a.href.replace('mailto:','')),
                cfemails: [...el.querySelectorAll('[data-cfemail]')].map(e => e.getAttribute('data-cfemail')),
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
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if not lines:
                continue
<<<<<<< HEAD
            name_candidate = lines[0]
            title_candidate = _TITLE_CLEANUP_RE.sub("", " ".join(lines[1:3])).strip()
            classification = art_classifier.classify(title_candidate)
            if not permissive and not classification.is_art:
                continue
            if len(name_candidate.split()) < 2 or len(name_candidate) > 60:
                continue
=======
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
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb
            item_emails = item.get("emails", [])
            cfemails = item.get("cfemails", [])
            msg_link = item.get("msgLink", "")
<<<<<<< HEAD
            raw_email = item_emails[0] if item_emails else (cfemails[0] if cfemails else "")
            email, source = _resolve_email(raw_email if item_emails else "", cfemails[0] if cfemails else "", raw)
            if not email and msg_link:
                email, source = msg_link, None
            if permissive and not email:
                continue
            records.append(StaffRecord(
                name=name_candidate, title=title_candidate, email=email,
                discipline=classification.discipline, email_source=source,
                evidence_url=evidence_url,
            ))
=======
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
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb

    seen: set[str] = set()
    unique: list[StaffRecord] = []
    for r in records:
        key = r.name.lower()
        if key not in seen:
            seen.add(key)
            unique.append(r)
    return unique


def _resolve_email(mailto_or_form: str, cfhex: str, raw_text: str) -> tuple[Optional[str], Optional[str]]:
    """Stage 4 email decoding, in priority order: real mailto -> Cloudflare
    hex -> textual munge in surrounding text -> "send a message" form link
    (not a real address; caller treats this as contact_form_only).
    """
    if mailto_or_form and EMAIL_RE.fullmatch(mailto_or_form):
        return mailto_or_form, "MAILTO"
    if cfhex:
        decoded = ed.decode_cf_email(cfhex)
        if decoded.email:
            return decoded.email, decoded.source
    munged = ed.decode_textual_munge(raw_text)
    if munged.email:
        return munged.email, munged.source
    if mailto_or_form:  # a contact-form / "send a message" URL, not an address
        return mailto_or_form, None
    return None, None


async def scrape_school(
    browser: Browser,
    school: dict,
    robots: RobotsCache | None = None,
    session: aiohttp.ClientSession | None = None,
    district_dir_cache: dict[str, pd.PathCandidate] | None = None,
) -> tuple[list[StaffRecord], str, list[dict], er.EntityResolution]:
    """Scrape one school. Returns (records, status, paths_attempted, resolution).

    Stage 1 runs first: a DISTRICT/PARENT_ORG/PROGRAM/AMBIGUOUS/NOT_A_K12
    verdict short-circuits before any HTTP request other than the NCES
    lookup (which is local, not a network call).
    """
    resolution = er.resolve(school["school_name"], state_hint=school.get("state"))

    if resolution.entity_type == "PARENT_ORG":
        return [], STATUS_NOT_A_SCHOOL, [], resolution
    if resolution.entity_type == "AMBIGUOUS":
        return [], STATUS_AMBIGUOUS_ENTITY, [], resolution
    if resolution.entity_type == "NOT_A_K12":
        return [], STATUS_NOT_A_SCHOOL, [], resolution

    url = school["website_url"]
    program_redirected = False
    if resolution.entity_type == "PROGRAM":
        if resolution.domain:
            url = f"https://{resolution.domain}"
            program_redirected = True
        # else: no known host-school domain — fall through and try the
        # school's own website_url if ingest happened to have one.

    if not url:
        return [], STATUS_NO_DIRECTORY_FOUND, [], resolution

    if robots is None:
        robots = RobotsCache()
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession(headers={"User-Agent": SCRAPER_UA})

    if not await robots.can_fetch(url, session):
        if own_session:
            await session.close()
        return [], STATUS_BLOCKED, [], resolution

    district = school.get("district_name")
    cached = district_dir_cache.get(district) if (district_dir_cache and district) else None

    context = await browser.new_context(user_agent=SCRAPER_UA)
    page = await context.new_page()
    try:
        dir_url, attempts, source = await find_directory(page, url, session, robots, cached)
        if not dir_url:
            return [], STATUS_NO_DIRECTORY_FOUND, attempts, resolution

        if source in ("sitemap", "cms:blackboard", "cms:finalsite", "cms:edlio", "cms:apptegy",
                      "cms:schoolmessenger", "cms:wordpress") and district_dir_cache is not None and district:
            district_dir_cache[district] = pd.PathCandidate(url=dir_url, source=source, priority=1)

        try:
            await page.goto(dir_url, timeout=20000, wait_until="domcontentloaded")
        except PWTimeout:
            return [], STATUS_TIMEOUT, attempts, resolution

        body_text = await page.inner_text("body")
        has_password = await page.query_selector("input[type=password]") is not None
        if acc.looks_like_auth_wall(body_text, has_password):
            return [], STATUS_AUTH_REQUIRED, attempts, resolution

        # Stage 3: blank-submit search form (NEISD-style directories).
        if await page.query_selector("form") is not None and not (EMAIL_RE.search(body_text) or STAFF_KEYWORD_RE.search(body_text)):
            await acc.submit_blank_form(page)
            await acc.wait_for_dynamic_content(page, "body")

        await asyncio.sleep(1.0)
        records = await _extract_staff(page, dir_url, permissive=False)

        # A-Z letter index (Burnham Wood-style): visit every present letter,
        # not just the default view — an absent letter means no results,
        # not that the index wasn't checked.
        html = await page.content()
        letters = acc.find_letter_anchors(html)
        if letters and not records:
            for letter in letters:
                try:
                    anchor = await page.query_selector(f"a:has-text('{letter}')")
                    if anchor:
                        await anchor.click(timeout=3000)
                        await asyncio.sleep(0.5)
                        records.extend(await _extract_staff(page, dir_url, permissive=False))
                except Exception:
                    continue

        # Numbered pagination.
        if records:
            records.extend(await acc.iterate_pagination(
                page, dir_url, lambda p: _extract_staff(p, dir_url, permissive=False)
            ))

        # Even on a normal staff directory, fall back to the arts department
        # page (permissive extraction) if no one matched.
        arts_page_used = False
        if not records:
            dept_candidates = [pd.PathCandidate(url=url.rstrip("/") + p, source="department", priority=6) for p in pd.DEPARTMENT_PATHS]
            arts_match = lambda text: bool(ARTS_KEYWORD_RE.search(text) and (EMAIL_RE.search(text) or STAFF_KEYWORD_RE.search(text)))
            hit, dept_attempts = await pd.probe_candidates(session, dept_candidates, robots, arts_match)
            attempts.extend(a.__dict__ for a in dept_attempts)
            if hit:
                await page.goto(hit.url, timeout=20000, wait_until="domcontentloaded")
                await asyncio.sleep(1.0)
                records = await _extract_staff(page, hit.url, permissive=True)
                arts_page_used = bool(records)

        seen = {r.name.lower() for r in records}
        deduped = []
        for r in records:
            if r.name.lower() in seen:
                deduped.append(r)
                seen.discard(r.name.lower())
        records = deduped

        if not records:
            status = STATUS_NO_TEACHERS_LISTED
        elif program_redirected:
            status = STATUS_PROGRAM_REDIRECTED
        else:
            status = STATUS_OK
        return records, status, attempts, resolution
    except PWTimeout:
        return [], STATUS_TIMEOUT, [], resolution
    except Exception as exc:
        log.warning("  Scrape error for %s: %s", url, exc)
        return [], f"{STATUS_ERROR_PREFIX} {exc}", [], resolution
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
<<<<<<< HEAD
                INSERT OR IGNORE INTO staff
                    (school_id, teacher_name, title, email, resolution_method,
                     discipline, email_source, email_verified, evidence_url)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    school_id, r.name, r.title, r.email,
                    "scraped" if r.email else "unresolved",
                    r.discipline, r.email_source, int(bool(r.email_verified)), r.evidence_url,
                ),
=======
                INSERT OR IGNORE INTO staff (school_id, teacher_name, title, email, resolution_method, added_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (school_id, r.name, r.title, r.email, "scraped" if r.email else "unresolved", now),
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                saved += 1
    return saved


def _save_resolution(school_id: int, resolution: er.EntityResolution, status: str, paths_attempted: list[dict]) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE schools SET
                entity_type=?, parent_entity=?, parent_entity_type=?,
                resolution_confidence=?, resolution_note=?, domain=?,
                paths_attempted=?, status=?, needs_human_review=?
            WHERE id=?
            """,
            (
                resolution.entity_type, resolution.parent_entity, resolution.parent_entity_type,
                resolution.resolution_confidence, resolution.resolution_note, resolution.domain,
                json.dumps(paths_attempted), status, int(resolution.needs_human_review),
                school_id,
            ),
        )


async def run_scraper(
    states: list[str],
    limit: int | None = None,
    rescrape_missed: bool = False,
    on_progress=None,
    should_stop=None,
    concurrency: int | None = None,
) -> None:
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
<<<<<<< HEAD
        q = "SELECT id, school_name, website_url, district_name, state FROM schools WHERE scraped=0 AND state IN ({})".format(
=======
        q = """SELECT id, school_name, website_url, district_name FROM schools
               WHERE scraped=0 AND state IN ({})
               ORDER BY is_arts_school DESC, school_name""".format(
>>>>>>> e9f2fcf5895baf274623909140806a9371b7fedb
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
    district_dir_cache: dict[str, pd.PathCandidate] = {}

    def _domain(url: str) -> str:
        return urllib.parse.urlparse(url).netloc.lower()

    async def _throttle(url: str) -> None:
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
                    records, status, paths_attempted, resolution = await scrape_school(
                        browser, school_dict, robots, session, district_dir_cache,
                    )
                    log.info("  → %d art teacher(s) found. Status: %s (entity: %s)",
                              len(records), status, resolution.entity_type)
                    saved = save_staff(school_dict["id"], records)
                    _save_resolution(school_dict["id"], resolution, status, paths_attempted)
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
        help="Reset missed schools (NO_DIRECTORY_FOUND, TIMEOUT, BLOCKED, error:*) and scrape everything.",
    )
    args = parser.parse_args()
    asyncio.run(run_scraper([s.upper() for s in args.states], args.limit, args.rescrape))


if __name__ == "__main__":
    main()
