"""
Retroactive contact-form checker.

For every staff record that still has no email (or needs re-checking),
revisit the school's staff directory and exhaustively look for any contact path:
  - mailto links
  - "Send a Message" buttons (including Apptegy modal buttons with name in text)
  - any link/button with contact-related keywords
  - obfuscated emails in text or data attributes
  - profile/bio pages with contact info

Handles pagination automatically.

Usage:
    python -m src.contact_forms --states KS [--limit 10]
    python -m src.contact_forms --states KS --reset  # re-check already-found contact_form records too
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import time
from typing import NamedTuple

import aiohttp
from playwright.async_api import async_playwright, Browser, Page, TimeoutError as PWTimeout

from .db import get_conn
from .scraper import (
    RobotsCache,
    SCRAPER_UA,
    DELAY_BETWEEN_REQUESTS,
    _find_directory_page,
    EMAIL_RE,
)

log = logging.getLogger(__name__)

# Matches button/link text that indicates a contact option
_CONTACT_TEXT_RE = re.compile(
    r"\b(send\s+a?\s*message|contact\s+me|email\s+me|message\s+me|send\s+email"
    r"|reach\s+out|get\s+in\s+touch|connect\s+with|contact|email)\b",
    re.IGNORECASE,
)

# "Send Messageto Karen Burrows" / "Send Message to Karen Burrows" (Apptegy pattern)
_APPTEGY_BTN_RE = re.compile(r"send\s*message\s*to\s+(.+)$", re.IGNORECASE)

# Pagination param pattern
_PAGINATION_RE = re.compile(r"[?&](page_no|page|p)=(\d+)", re.IGNORECASE)

# Obfuscated email: "name [at] domain [dot] edu"
_OBFUSCATED_EMAIL_RE = re.compile(
    r"[\w._%+\-]+\s*\[?at\]?\s*[\w.\-]+\s*\[?dot\]?\s*[a-z]{2,}",
    re.IGNORECASE,
)


class ContactResult(NamedTuple):
    value: str          # email address or URL of contact form/page
    method: str         # 'email', 'contact_form', 'send_message_button'


def _normalize(name: str) -> str:
    return " ".join(name.lower().split())


async def _get_all_page_urls(page: Page, dir_url: str) -> list[str]:
    """Return dir_url plus all paginated variants found on the page."""
    urls = [dir_url]
    try:
        all_hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
        param_name: str | None = None
        max_page = 1
        base = dir_url
        for href in all_hrefs:
            m = _PAGINATION_RE.search(href)
            if m:
                pn, num = m.group(1), int(m.group(2))
                if num > max_page:
                    max_page = num
                    param_name = pn
                    base = re.sub(r"[?&]" + pn + r"=\d+", "", href).rstrip("?&")
        if param_name and max_page > 1:
            sep = "&" if "?" in base else "?"
            for pnum in range(2, max_page + 1):
                urls.append(f"{base}{sep}{param_name}={pnum}")
    except Exception as exc:
        log.debug("Pagination scan failed: %s", exc)
    return urls


async def _extract_contacts_from_page(page: Page, page_url: str) -> dict[str, ContactResult]:
    """
    Full exhaustive contact extraction from a single page.
    Returns {normalized_teacher_name: ContactResult}.

    Strategies (in order of reliability):
    1. mailto: links anywhere on page
    2. Apptegy-style buttons: "Send Message to [Name]" in button text
    3. Any button/link with contact text, matched to nearest name in container
    4. Obfuscated email patterns in text
    5. Text-level mailto in raw HTML attributes (data-email, data-mailto, etc.)
    """
    results: dict[str, ContactResult] = {}

    # --- Strategy 1: mailto links ---
    try:
        mailto_links = await page.eval_on_selector_all(
            "a[href^='mailto:']",
            """els => els.map(el => ({
                email: el.href.replace('mailto:', '').split('?')[0].trim(),
                container: (
                    el.closest('[class*="staff"],[class*="person"],[class*="card"],[class*="employee"],tr,li')
                    || el.closest('div,p')
                    || el.parentElement
                ).innerText.trim()
            }))""",
        )
        for item in mailto_links:
            email = item.get("email", "").strip()
            container = item.get("container", "")
            if email and EMAIL_RE.match(email):
                # Try to extract name from container (first line is usually the name)
                lines = [l.strip() for l in container.splitlines() if l.strip()]
                if lines:
                    norm = _normalize(lines[0])
                    if norm and norm not in results:
                        results[norm] = ContactResult(value=email, method="email")
    except Exception as exc:
        log.debug("Strategy 1 (mailto) failed: %s", exc)

    # --- Strategy 2: Apptegy "Send Message to [Name]" buttons ---
    # Button text directly encodes the teacher name: "Send Messageto Karen Burrows"
    try:
        btn_texts = await page.eval_on_selector_all(
            "button, [role='button']",
            "els => els.map(e => e.textContent.trim().replace(/\\s+/g, ' '))",
        )
        for text in btn_texts:
            m = _APPTEGY_BTN_RE.search(text)
            if m:
                name = m.group(1).strip()
                norm = _normalize(name)
                if norm and norm not in results:
                    results[norm] = ContactResult(value=page_url, method="send_message_button")
    except Exception as exc:
        log.debug("Strategy 2 (Apptegy buttons) failed: %s", exc)

    # --- Strategy 3: Generic contact buttons/links matched via container text ---
    try:
        contact_elements = await page.eval_on_selector_all(
            "a[href], button, [role='button'], input[type='button'], input[type='submit']",
            r"""els => els
                .filter(el => {
                    const t = (el.textContent || el.value || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                    return /send\s*a?\s*message|contact\s*me|email\s*me|message\s*me|send\s*email|reach\s*out|get\s*in\s*touch|connect\s*with/i.test(t);
                })
                .map(el => ({
                    href: el.href || '',
                    text: (el.textContent || el.value || '').trim().replace(/\s+/g, ' '),
                    aria: el.getAttribute('aria-label') || '',
                    container: (
                        el.closest('[class*="staff"],[class*="person"],[class*="card"],[class*="employee"],tr,li')
                        || el.closest('section,article,div')
                        || el.parentElement
                    ).innerText.trim()
                }))
            """,
        )
        for item in contact_elements:
            href = item.get("href", "") or page_url
            container = item.get("container", "")
            lines = [l.strip() for l in container.splitlines() if l.strip()]
            if lines:
                norm = _normalize(lines[0])
                if norm and norm not in results:
                    method = "email" if href.startswith("mailto:") else "send_message_button"
                    value = href.replace("mailto:", "").split("?")[0] if href.startswith("mailto:") else (href or page_url)
                    results[norm] = ContactResult(value=value, method=method)
    except Exception as exc:
        log.debug("Strategy 3 (generic contact elements) failed: %s", exc)

    # --- Strategy 4: HTML data attributes with email info ---
    try:
        data_emails = await page.eval_on_selector_all(
            "[data-email], [data-mail], [data-mailto], [data-contact-email]",
            """els => els.map(el => ({
                email: el.getAttribute('data-email') || el.getAttribute('data-mail') || el.getAttribute('data-mailto') || el.getAttribute('data-contact-email') || '',
                container: (el.closest('[class*=\"staff\"],[class*=\"person\"],[class*=\"card\"]') || el.parentElement).innerText.trim()
            }))""",
        )
        for item in data_emails:
            email = item.get("email", "").strip()
            container = item.get("container", "")
            if email and EMAIL_RE.match(email):
                lines = [l.strip() for l in container.splitlines() if l.strip()]
                if lines:
                    norm = _normalize(lines[0])
                    if norm and norm not in results:
                        results[norm] = ContactResult(value=email, method="email")
    except Exception as exc:
        log.debug("Strategy 4 (data attributes) failed: %s", exc)

    # --- Strategy 5: Obfuscated email in visible text ---
    try:
        body_text = await page.inner_text("body")
        for match in _OBFUSCATED_EMAIL_RE.finditer(body_text):
            raw = match.group(0)
            email = re.sub(r"\s*\[?at\]?\s*", "@", raw, flags=re.IGNORECASE)
            email = re.sub(r"\s*\[?dot\]?\s*", ".", email, flags=re.IGNORECASE)
            email = email.strip()
            # Find teacher name in surrounding text
            start = max(0, match.start() - 200)
            context = body_text[start:match.start()]
            lines = [l.strip() for l in context.splitlines() if l.strip()]
            if lines:
                norm = _normalize(lines[-1])
                if norm and norm not in results:
                    results[norm] = ContactResult(value=email, method="email")
    except Exception as exc:
        log.debug("Strategy 5 (obfuscated email) failed: %s", exc)

    return results


async def _check_school_exhaustively(
    browser: Browser,
    school: dict,
    teachers_no_contact: list[dict],
    robots: RobotsCache,
    session: aiohttp.ClientSession,
) -> dict[int, ContactResult]:
    """
    Visit every page of the school's staff directory and exhaustively find
    contact info for the given teachers. Returns {staff_id: ContactResult}.
    """
    url = school["website_url"]
    if not url:
        return {}
    if not await robots.can_fetch(url, session):
        log.info("  robots.txt blocked: %s", url)
        return {}

    context = await browser.new_context(user_agent=SCRAPER_UA)
    page = await context.new_page()
    updates: dict[int, ContactResult] = {}

    # Build lookup: normalized name → staff_id
    id_by_norm: dict[str, int] = {_normalize(t["teacher_name"]): t["id"] for t in teachers_no_contact}

    try:
        dir_url, _ = await _find_directory_page(page, url, session, robots)
        if not dir_url:
            log.info("  No directory page found for %s", url)
            return {}

        await page.goto(dir_url, timeout=20000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        # Discover all paginated URLs
        page_urls = await _get_all_page_urls(page, dir_url)
        log.info("  Directory: %s (%d page(s))", dir_url, len(page_urls))

        # Collect contact info from all pages
        all_contacts: dict[str, ContactResult] = {}

        for i, purl in enumerate(page_urls):
            if i > 0:
                try:
                    await page.goto(purl, timeout=20000, wait_until="domcontentloaded")
                    await asyncio.sleep(1.0)
                except PWTimeout:
                    log.debug("  Timeout on page %s", purl)
                    continue

            page_contacts = await _extract_contacts_from_page(page, purl)
            all_contacts.update(page_contacts)

        # Match collected contacts to our teachers
        for norm_teacher, staff_id in id_by_norm.items():
            # Exact match first
            if norm_teacher in all_contacts:
                updates[staff_id] = all_contacts[norm_teacher]
                log.info("    Match (exact): %s → %s", norm_teacher, all_contacts[norm_teacher].value)
                continue

            # Fuzzy: check if any word from the teacher name appears in any contact name
            teacher_parts = set(norm_teacher.split())
            for contact_norm, result in all_contacts.items():
                contact_parts = set(contact_norm.split())
                shared = teacher_parts & contact_parts
                # Match if at least 2 name parts overlap (first + last)
                if len(shared) >= 2 or (len(shared) >= 1 and len(teacher_parts) == 1):
                    if staff_id not in updates:
                        updates[staff_id] = result
                        log.info("    Match (fuzzy %s↔%s): %s → %s", norm_teacher, contact_norm, norm_teacher, result.value)

    except PWTimeout:
        log.warning("  Timeout for %s", url)
    except Exception as exc:
        log.warning("  Error for %s: %s", url, exc)
    finally:
        await context.close()

    return updates


async def run_contact_form_check(
    states: list[str],
    limit: int | None = None,
    reset: bool = False,
) -> None:
    where_extra = "" if reset else "AND (s.email IS NULL OR s.email = '')"
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT s.id, s.teacher_name, sc.id AS school_id, sc.school_name, sc.website_url
            FROM staff s
            JOIN schools sc ON sc.id = s.school_id
            WHERE sc.state IN ({','.join('?' * len(states))})
              {where_extra}
            ORDER BY sc.id, s.id
            """,
            states,
        ).fetchall()

    if limit:
        rows = rows[:limit]

    # Group by school
    schools: dict[int, dict] = {}
    teachers_by_school: dict[int, list[dict]] = {}
    for row in rows:
        r = dict(row)
        sid = r["school_id"]
        if sid not in schools:
            schools[sid] = {"id": sid, "school_name": r["school_name"], "website_url": r["website_url"]}
            teachers_by_school[sid] = []
        teachers_by_school[sid].append({"id": r["id"], "teacher_name": r["teacher_name"]})

    log.info(
        "Checking %d school(s) (%d staff record(s)) for contact info…",
        len(schools),
        len(rows),
    )

    robots = RobotsCache()
    total_updated = 0

    async with aiohttp.ClientSession(headers={"User-Agent": SCRAPER_UA}) as session:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            for i, (school_id, school) in enumerate(schools.items(), 1):
                teachers = teachers_by_school[school_id]
                log.info(
                    "[%d/%d] %s (%s) — %d teacher(s) to resolve",
                    i, len(schools),
                    school["school_name"], school["website_url"],
                    len(teachers),
                )
                updates = await _check_school_exhaustively(browser, school, teachers, robots, session)
                if updates:
                    with get_conn() as conn:
                        for staff_id, result in updates.items():
                            conn.execute(
                                "UPDATE staff SET email=?, resolution_method=? WHERE id=?",
                                (result.value, result.method, staff_id),
                            )
                    log.info("  → Updated %d record(s).", len(updates))
                    total_updated += len(updates)
                else:
                    log.info("  → No contact info found.")

                if i < len(schools) and school["website_url"]:
                    time.sleep(await robots.crawl_delay(school["website_url"], session))

            await browser.close()

    log.info("Done. %d staff record(s) updated.", total_updated)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Check for contact info for staff with no email.")
    parser.add_argument("--states", nargs="+", default=["KS"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--reset", action="store_true", help="Re-check records already marked contact_form")
    args = parser.parse_args()
    asyncio.run(run_contact_form_check([s.upper() for s in args.states], args.limit, args.reset))


if __name__ == "__main__":
    main()
