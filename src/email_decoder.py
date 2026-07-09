"""
Stage 4 — Contact extraction / email decoding.

Every decoder returns (email, source) or (None, reason) — never a guess
dressed up as a fact. `email_source` on the output record must be one of
MAILTO | CF_DECODED | JS_DECODED | OCR | INFERRED, matching the pipeline's
output contract.

The general-purpose answer to "some custom JS cipher obfuscates this email"
is `decode_via_js_execution`: don't reverse-engineer the cipher, execute the
page's own JS and read back the DOM/mailto href it produces. That's what
generalizes to schemes we haven't seen (Burnham Wood's `--_...;bv:...rg_--`
wrappers included) instead of writing one bespoke regex per site.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


@dataclass
class DecodedEmail:
    email: str | None
    source: str | None  # MAILTO | CF_DECODED | JS_DECODED | OCR | INFERRED | None
    confidence: str = "high"  # "high" | "low" (OCR, inference)
    note: str = ""


# ---------------------------------------------------------------------------
# Cloudflare email protection
# ---------------------------------------------------------------------------

def decode_cf_email(cfhex: str) -> DecodedEmail:
    """Decode a Cloudflare `data-cfemail` hex-encoded, single-byte-XOR
    obfuscated address, e.g. <span data-cfemail="1e...">.
    """
    cfhex = cfhex.strip()
    try:
        raw = bytes.fromhex(cfhex)
    except ValueError:
        return DecodedEmail(email=None, source=None, note="not valid hex")
    if len(raw) < 2:
        return DecodedEmail(email=None, source=None, note="too short to be cf-encoded")
    key = raw[0]
    decoded = bytes(b ^ key for b in raw[1:]).decode("utf-8", errors="replace")
    if not EMAIL_RE.fullmatch(decoded):
        return DecodedEmail(email=None, source=None, note=f"decoded to non-email: {decoded!r}")
    return DecodedEmail(email=decoded, source="CF_DECODED")


# ---------------------------------------------------------------------------
# Textual munging: "name [at] domain [dot] org", zero-width chars, rtl reverse
# ---------------------------------------------------------------------------

# Both "at" and "dot" munged: "name [at] domain [dot] org"
_MUNGED_AT_DOT_RE = re.compile(
    r"([\w._%+\-]+)\s*[\[\(]?\s*at\s*[\]\)]?\s*([\w.\-]+)\s*[\[\(]?\s*dot\s*[\]\)]?\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)
# Only "at" munged, domain.tld left as a literal dot: "name(at)domain.org"
_MUNGED_AT_ONLY_RE = re.compile(
    r"([\w._%+\-]+)\s*[\[\(]\s*at\s*[\]\)]\s*([\w.\-]+\.[a-zA-Z]{2,})",
    re.IGNORECASE,
)
_ZERO_WIDTH_RE = re.compile("[​‌‍﻿]")


def decode_textual_munge(text: str) -> DecodedEmail:
    """Handle 'name [at] domain [dot] org', zero-width-char-inserted
    addresses, and CSS-reversed (direction: rtl) text passed in already
    reversed by the caller.
    """
    cleaned = _ZERO_WIDTH_RE.sub("", text or "")

    direct = EMAIL_RE.search(cleaned)
    if direct:
        return DecodedEmail(email=direct.group(0), source="MAILTO", note="plain address after zero-width strip")

    m = _MUNGED_AT_DOT_RE.search(cleaned)
    if m:
        local, domain, tld = m.groups()
        return DecodedEmail(email=f"{local}@{domain}.{tld}", source="JS_DECODED", note="textual at/dot munge")

    m = _MUNGED_AT_ONLY_RE.search(cleaned)
    if m:
        local, domain = m.groups()
        return DecodedEmail(email=f"{local}@{domain}", source="JS_DECODED", note="textual at munge")

    return DecodedEmail(email=None, source=None, note="no recognizable pattern")


def reverse_rtl_text(text: str) -> str:
    """CSS `direction: rtl` addresses are stored forwards in the DOM but
    rendered backwards; the raw text needs reversing before pattern-matching."""
    return text[::-1]


# ---------------------------------------------------------------------------
# onclick-handler-built addresses
# ---------------------------------------------------------------------------

# Common pattern: onclick="location.href='mailto:'+'name'+'@'+'domain.org'"
_ONCLICK_MAILTO_RE = re.compile(r"mailto:([^'\"]+)", re.IGNORECASE)
_ONCLICK_CONCAT_RE = re.compile(r"['\"]([^'\"]*)['\"]")


def decode_onclick_email(onclick_attr: str) -> DecodedEmail:
    """Best-effort static decode of a simple string-concatenation onclick
    handler. This only covers the trivial case (quoted-literal concatenation
    with no computed values); anything fancier should go through
    `decode_via_js_execution` instead, which is the general solution.
    """
    if not onclick_attr:
        return DecodedEmail(email=None, source=None, note="empty onclick")

    m = _ONCLICK_MAILTO_RE.search(onclick_attr)
    if m:
        candidate = m.group(1)
        if EMAIL_RE.fullmatch(candidate):
            return DecodedEmail(email=candidate, source="JS_DECODED", note="onclick mailto literal")

    literals = _ONCLICK_CONCAT_RE.findall(onclick_attr)
    joined = "".join(literals)
    email_match = EMAIL_RE.search(joined)
    if email_match:
        return DecodedEmail(email=email_match.group(0), source="JS_DECODED", note="onclick string-concat literals")

    return DecodedEmail(
        email=None, source=None,
        note="onclick handler builds address from computed values; needs decode_via_js_execution",
    )


# ---------------------------------------------------------------------------
# General solution: execute the page's own JS and read the result back.
#
# This is the one strategy that generalizes to obfuscation schemes we
# haven't seen (Burnham Wood's `--_...;bv:...rg_--` wrappers included):
# rather than reverse-engineering each site's cipher, click/trigger the
# element in a real page context and read whatever mailto href or DOM text
# the site's own decoder produces.
# ---------------------------------------------------------------------------

async def decode_via_js_execution(page, selector: str) -> DecodedEmail:
    """Click (or otherwise trigger) the element at `selector` in a live
    Playwright page and read back the email it reveals — either a `mailto:`
    href it navigates to/creates, or plain email text that appears in the
    DOM. Requires a Playwright `Page` already positioned on the directory.
    """
    try:
        handle = await page.query_selector(selector)
        if handle is None:
            return DecodedEmail(email=None, source=None, note=f"selector not found: {selector}")

        # If it's already a mailto anchor, no need to click/execute anything.
        href = await handle.get_attribute("href")
        if href and href.lower().startswith("mailto:"):
            addr = href.split(":", 1)[1].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                return DecodedEmail(email=addr, source="MAILTO")

        before_html = await page.content()
        try:
            await handle.click(timeout=3000)
        except Exception:
            pass  # not everything is clickable; still check for DOM changes below

        after_href = await handle.get_attribute("href")
        if after_href and after_href.lower().startswith("mailto:"):
            addr = after_href.split(":", 1)[1].split("?")[0].strip()
            if EMAIL_RE.fullmatch(addr):
                return DecodedEmail(email=addr, source="JS_DECODED", note="href set by click handler")

        after_html = await page.content()
        if after_html != before_html:
            found = EMAIL_RE.findall(after_html)
            if found:
                return DecodedEmail(email=found[0], source="JS_DECODED", note="revealed in DOM after trigger")

        # Last resort: whatever the element's own text now reads.
        text = await handle.inner_text()
        found = EMAIL_RE.findall(text or "")
        if found:
            return DecodedEmail(email=found[0], source="JS_DECODED", note="revealed in element text after trigger")

        return DecodedEmail(email=None, source=None, note="triggered but no email surfaced")
    except Exception as exc:
        return DecodedEmail(email=None, source=None, note=f"js-execution decode failed: {exc}")


# ---------------------------------------------------------------------------
# Images of email addresses (OCR) — confidence: low, always
# ---------------------------------------------------------------------------

def ocr_email_from_image(image_bytes: bytes) -> DecodedEmail:
    """OCR an <img> that renders an email address as a picture, to defeat
    scrapers. Requires pytesseract + a local Tesseract binary; neither is a
    hard dependency of this project (Tesseract isn't pip-installable), so
    this degrades explicitly instead of silently returning nothing.
    """
    try:
        import io

        import pytesseract
        from PIL import Image
    except ImportError as exc:
        return DecodedEmail(
            email=None, source=None,
            note=f"OCR unavailable: {exc}. Install pytesseract + the Tesseract binary to enable.",
        )

    try:
        img = Image.open(io.BytesIO(image_bytes))
        text = pytesseract.image_to_string(img)
    except Exception as exc:
        return DecodedEmail(email=None, source=None, note=f"OCR failed: {exc}")

    found = EMAIL_RE.findall(text)
    if not found:
        return DecodedEmail(email=None, source=None, note="OCR produced no email-shaped text")
    return DecodedEmail(email=found[0], source="OCR", confidence="low", note="OCR result — verify before use")


# ---------------------------------------------------------------------------
# Pattern inference — never marked verified until SMTP/Hunter confirms it
# ---------------------------------------------------------------------------

def generate_pattern_candidates(first: str, last: str, domain: str) -> list[str]:
    first, last = first.lower(), last.lower()
    if not first or not last or not domain:
        return []
    return [
        f"{first}.{last}@{domain}",
        f"{first[0]}{last}@{domain}",
        f"{first}{last}@{domain}",
        f"{first}_{last}@{domain}",
        f"{first[0]}.{last}@{domain}",
    ]


def infer_from_verified_pattern(
    first: str, last: str, domain: str, verified_addresses: list[str]
) -> DecodedEmail:
    """Infer an address from a district's already-*verified* addresses'
    consistent pattern. Always returns email_source=INFERRED and must not be
    treated as `email_verified: true` until it independently passes
    SMTP/Hunter verification — an inferred, unverified address is a failure,
    not a result, per spec.
    """
    patterns_seen = set()
    for addr in verified_addresses:
        local = addr.split("@")[0].lower()
        if "." in local:
            patterns_seen.add("first.last")
        elif len(local) > 1 and local[0].isalpha() and local[1:].isalnum():
            patterns_seen.add("flast")
    if len(patterns_seen) != 1:
        return DecodedEmail(
            email=None, source=None,
            note="verified addresses don't share one consistent pattern; refusing to infer",
        )
    pattern = next(iter(patterns_seen))
    first, last = first.lower(), last.lower()
    email = f"{first}.{last}@{domain}" if pattern == "first.last" else f"{first[0]}{last}@{domain}"
    return DecodedEmail(
        email=email, source="INFERRED", confidence="low",
        note="inferred from district pattern; must pass SMTP/Hunter verification before being marked verified",
    )
