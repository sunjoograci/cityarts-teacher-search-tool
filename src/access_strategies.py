"""
Stage 3 — Access barriers.

One handler per barrier, each usable/testable independently. The
Playwright-driving functions (submit_blank_form, iterate_filter_options,
wait_for_dynamic_content, iterate_pagination, recurse_iframes) need a live
`Page` and aren't unit-testable without a browser; the parsing/detection
helpers each function is built on (`find_letter_anchors`, `find_pagination`,
`looks_like_auth_wall`, `RateLimiter`) are pure and are covered directly.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

# Cap for every "networkidle" wait in this module. networkidle is treated as
# a best-effort nicety, never the real signal — some sites (persistent
# analytics/chat-widget polling, e.g. ideapublicschools.org) never go idle
# at all, so a networkidle wait there always burns its *entire* timeout
# before falling through to the real fallback (a selector wait or a fixed
# sleep). Measured directly: ideapublicschools.org loads in ~1s but never
# reaches networkidle even after 15s. Keeping this short (not 8-15s per
# call) is what keeps a handful of chatty domains from eating the whole
# per-school time budget when several of these calls stack in one crawl.
_NETWORKIDLE_CAP_MS = 3000

# ---------------------------------------------------------------------------
# Blank-submit search forms (NEISD: directory only populates after
# submitting the search form with every field empty)
# ---------------------------------------------------------------------------

async def submit_blank_form(page) -> bool:
    """Find the first <form> on the current page and submit it with all
    fields left empty. Returns True if a form was found and submitted.

    Prefers intercepting the resulting network response (see
    `submit_blank_form_and_capture_json`) when the underlying endpoint is a
    JSON API — plain form submission is the fallback for server-rendered
    results.
    """
    form = await page.query_selector("form")
    if form is None:
        return False
    submit_btn = await form.query_selector("button[type=submit], input[type=submit]")
    try:
        if submit_btn:
            await submit_btn.click()
        else:
            await page.eval_on_selector("form", "f => f.submit()")
        await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
    except Exception:
        pass
    return True


async def submit_blank_form_and_capture_json(page, timeout: float = 10.0) -> list[dict] | None:
    """Same as submit_blank_form, but listens for an XHR/fetch response that
    looks like a JSON staff list and returns it directly instead of relying
    on the DOM having re-rendered — the general fix for directories whose
    search form calls a JSON API under the hood.
    """
    captured: list[dict] = []

    def on_response(response):
        if "json" in (response.headers.get("content-type") or "").lower():
            captured.append({"url": response.url, "response": response})

    page.on("response", on_response)
    try:
        submitted = await submit_blank_form(page)
        if not submitted:
            return None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not captured:
            await asyncio.sleep(0.25)
        if not captured:
            return None
        data = await captured[-1]["response"].json()
        return data if isinstance(data, list) else data.get("results") or data.get("data") or [data]
    except Exception:
        return None
    finally:
        page.remove_listener("response", on_response)


# ---------------------------------------------------------------------------
# Required filter selection (campus/department dropdowns)
# ---------------------------------------------------------------------------

async def enumerate_select_options(page, select_selector: str) -> list[str]:
    """Return every <option> value under a <select> (skipping the blank
    placeholder), so the caller can iterate all of them rather than assume
    the default/first option returns everything.
    """
    try:
        return await page.eval_on_selector_all(
            f"{select_selector} option",
            "els => els.map(e => e.value).filter(v => v && v.trim())",
        )
    except Exception:
        return []


async def select_and_wait(page, select_selector: str, value: str) -> None:
    await page.select_option(select_selector, value)
    try:
        await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
    except Exception:
        await asyncio.sleep(1.0)


# ---------------------------------------------------------------------------
# Client-side rendering: wait for real content, not a fixed timeout
# ---------------------------------------------------------------------------

async def wait_for_dynamic_content(page, content_selector: str, timeout: int = 8000) -> bool:
    """Wait for the presence of a content selector — the real signal that
    something rendered. `networkidle` is tried first but only ever gets a
    short, fixed budget (see _NETWORKIDLE_CAP_MS): it's a nicety when it
    works and a guaranteed no-op timeout on sites with persistent background
    network chatter, so it must never eat into the caller-supplied `timeout`
    that's meant for the selector wait itself.
    """
    try:
        await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
    except Exception:
        pass
    try:
        await page.wait_for_selector(content_selector, timeout=timeout, state="attached")
        return True
    except Exception:
        return False


def looks_like_staff_json(payload) -> bool:
    """Heuristic: does an XHR JSON payload look like a list of staff
    records? Used to decide whether to parse XHR JSON directly instead of
    the (possibly still-empty) DOM."""
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("data") or payload.get("staff") or []
    if not isinstance(payload, list) or not payload:
        return False
    sample = payload[0]
    if not isinstance(sample, dict):
        return False
    keys = {k.lower() for k in sample.keys()}
    return bool(keys & {"name", "firstname", "first_name", "lastname", "last_name", "title", "email"})


# ---------------------------------------------------------------------------
# Pagination: numbered pages, load-more, infinite scroll, A-Z letter anchors
# ---------------------------------------------------------------------------

_PAGE_PARAM_RE = re.compile(r"[?&](page_no|page|p)=(\d+)", re.IGNORECASE)


def find_pagination(hrefs: list[str]) -> tuple[str | None, int, str | None]:
    """Given all hrefs on a directory page, return (param_name, max_page,
    base_url_without_page_param) if numbered pagination is present."""
    param_name: str | None = None
    max_page = 1
    base = None
    for href in hrefs:
        m = _PAGE_PARAM_RE.search(href)
        if m:
            pn, num = m.group(1), int(m.group(2))
            if num > max_page:
                max_page = num
                param_name = pn
                base = re.sub(r"[?&]" + pn + r"=\d+", "", href).rstrip("?&")
    return param_name, max_page, base


def build_paginated_urls(param_name: str, max_page: int, base_url: str) -> list[str]:
    sep = "&" if "?" in base_url else "?"
    return [f"{base_url}{sep}{param_name}={n}" for n in range(2, max_page + 1)]


# A-Z letter index anchors (Burnham Wood: "B C F G H J L O P R S T V Z" —
# absent letters are absent from the index, not empty; every present letter
# must be visited).
_LETTER_LINK_RE = re.compile(r">\s*([A-Z])\s*<", re.IGNORECASE)


def find_letter_anchors(html_text: str) -> list[str]:
    """Return the distinct single-letter index anchors present on a page
    (e.g. an A-Z staff index), preserving encounter order.
    """
    seen: list[str] = []
    for m in re.finditer(r'<a\b[^>]*>\s*([A-Za-z])\s*</a>', html_text or ""):
        letter = m.group(1).upper()
        if letter not in seen:
            seen.append(letter)
    return seen


async def iterate_pagination(page, dir_url: str, extract_fn) -> list:
    """Visit every numbered page found via find_pagination, calling
    extract_fn(page) on each and concatenating results."""
    hrefs = await page.eval_on_selector_all("a[href]", "els => els.map(e => e.href)")
    param_name, max_page, base = find_pagination(hrefs)
    records = []
    if not param_name or max_page <= 1 or not base:
        return records
    for url in build_paginated_urls(param_name, max_page, base):
        try:
            await page.goto(url, timeout=20000, wait_until="domcontentloaded")
            await asyncio.sleep(1.0)
            records.extend(await extract_fn(page))
        except Exception:
            continue
    return records


async def click_load_more_until_exhausted(page, button_selector: str, max_clicks: int = 50) -> int:
    """Repeatedly click a 'load more' button until it disappears/disables or
    max_clicks is hit. Returns the number of clicks performed."""
    clicks = 0
    while clicks < max_clicks:
        btn = await page.query_selector(button_selector)
        if btn is None:
            break
        try:
            disabled = await btn.get_attribute("disabled")
            if disabled is not None:
                break
            await btn.click()
            clicks += 1
        except Exception:
            break
        # A networkidle timeout here is expected on chatty sites and must
        # NOT abort the loop (it used to — one timed-out wait would kill
        # every remaining "load more" click, even with more pages left).
        try:
            await page.wait_for_load_state("networkidle", timeout=_NETWORKIDLE_CAP_MS)
        except Exception:
            await asyncio.sleep(0.8)
    return clicks


async def scroll_until_stable(page, max_scrolls: int = 30, pause: float = 0.6) -> None:
    """Infinite-scroll handler: scroll to bottom repeatedly until page
    height stops growing."""
    last_height = None
    for _ in range(max_scrolls):
        height = await page.evaluate("document.body.scrollHeight")
        if height == last_height:
            break
        last_height = height
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(pause)


# ---------------------------------------------------------------------------
# iframes
# ---------------------------------------------------------------------------

def collect_all_frames(page) -> list:
    """Every frame on the page (main frame included), so extraction logic
    can recurse into iframe-embedded directories (a common pattern for
    third-party staff-directory widgets)."""
    return list(page.frames)


# ---------------------------------------------------------------------------
# PDF-only directories
# ---------------------------------------------------------------------------

@dataclass
class PdfExtractionResult:
    text: str
    method: str  # "text" | "ocr" | "unavailable"


def extract_pdf_text(pdf_bytes: bytes) -> PdfExtractionResult:
    """Extract text from a PDF directory. Falls back to noting OCR is
    needed for scanned (image-only) PDFs rather than fabricating text.
    """
    try:
        import io

        from pypdf import PdfReader
    except ImportError as exc:
        return PdfExtractionResult(text="", method="unavailable")

    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception:
        return PdfExtractionResult(text="", method="unavailable")

    if text.strip():
        return PdfExtractionResult(text=text, method="text")
    return PdfExtractionResult(text="", method="ocr")  # scanned PDF — caller should OCR each page image


# ---------------------------------------------------------------------------
# Auth walls — detect, do not attempt to bypass
# ---------------------------------------------------------------------------

_AUTH_WALL_RE = re.compile(
    r"\b(staff\s+login|sign\s*in\s+required|please\s+log\s*in|employee\s+portal|"
    r"single\s+sign[\s-]*on|sso\s+login)\b",
    re.IGNORECASE,
)


def looks_like_auth_wall(page_text: str, has_password_field: bool = False) -> bool:
    return bool(_AUTH_WALL_RE.search(page_text or "")) or has_password_field


# ---------------------------------------------------------------------------
# Bot challenges / rate limiting — exponential backoff, per-domain concurrency
# ---------------------------------------------------------------------------

_BOT_CHALLENGE_RE = re.compile(
    r"\b(checking\s+your\s+browser|cloudflare|are\s+you\s+a\s+robot|captcha|access\s+denied|rate\s+limit)\b",
    re.IGNORECASE,
)


def looks_like_bot_challenge(status: int, page_text: str) -> bool:
    return status in (403, 429, 503) or bool(_BOT_CHALLENGE_RE.search(page_text or ""))


class ExponentialBackoff:
    """Per-domain backoff state. `next_delay()` doubles the wait each time
    `record_failure()` is called and resets on `record_success()`."""

    def __init__(self, base_delay: float = 2.0, max_delay: float = 120.0) -> None:
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._delay = base_delay

    def record_failure(self) -> float:
        delay = self._delay
        self._delay = min(self._delay * 2, self.max_delay)
        return delay

    def record_success(self) -> None:
        self._delay = self.base_delay

    async def wait(self) -> None:
        await asyncio.sleep(self._delay)
