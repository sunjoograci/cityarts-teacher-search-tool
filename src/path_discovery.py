"""
Stage 2 — Path discovery.

Builds a ranked candidate list for a domain instead of assuming
`/staff-directory`, and returns a full attempt log (url, status,
rejected_because) regardless of whether a hit was found — a `NO_DIRECTORY_
FOUND` verdict is only legal once every source below has been tried.

Sources, in priority order (matches the project spec):
    1. sitemap.xml / sitemap_index.xml / robots.txt-declared sitemaps
    2. CMS fingerprint -> that CMS's known directory routes
    3. nav + footer link crawl (depth up to 3)
    4. site-scoped search engine query (site:{domain} staff OR faculty...)
    5. the site's own internal search box
    6. department pages (fine arts / visual arts / etc.)

The pure functions here (parse_sitemap_urls, detect_cms, extract_nav_links,
score_directory_link, ...) take plain text and are unit-testable without a
network call or a browser. `discover` is the async orchestrator that
actually fetches things.
"""
from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from xml.etree import ElementTree

# ---------------------------------------------------------------------------
# Shared vocab
# ---------------------------------------------------------------------------

DIRECTORY_SLUG_RE = re.compile(
    r"(staff|faculty|directory|team|personnel|employee|our-school|meet-the-team|"
    r"meet-our|contact|departments?|administration|teacher)",
    re.IGNORECASE,
)

DIRECTORY_LINK_TEXT_RE = re.compile(
    r"\b(staff\s+directory|faculty(\s*&\s*|\s+and\s+|\s+)?staff|faculty|our\s+staff|our\s+team|"
    r"meet\s+the\s+team|meet\s+our\s+teachers?|employee\s+directory|campus\s+staff|contact\s+us|"
    r"departments?|administration|fine\s+arts|specials|teacher\s+pages?|teacher\s+websites?)\b",
    re.IGNORECASE,
)

DEPARTMENT_PATHS = [
    "/fine-arts", "/visual-arts", "/departments/art", "/departments/fine-arts",
    "/departments/visual-arts", "/programs/fine-arts", "/programs/visual-arts",
    "/curriculum/fine-arts", "/arts", "/fine-arts-department",
]


@dataclass
class PathCandidate:
    url: str
    source: str          # "sitemap" | "cms:<name>" | "nav" | "site_search" | "internal_search" | "department"
    priority: int         # lower = tried first


@dataclass
class PathAttempt:
    url: str
    status: int | str | None
    rejected_because: str


@dataclass
class DiscoveryResult:
    candidates: list[PathCandidate] = field(default_factory=list)
    attempts: list[PathAttempt] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 1. Sitemap parsing
# ---------------------------------------------------------------------------

def parse_sitemap_urls(xml_text: str) -> list[str]:
    """Extract every <loc> URL from a sitemap or sitemap-index XML document."""
    urls: list[str] = []
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return urls
    for el in root.iter():
        if el.tag.endswith("loc") and el.text:
            urls.append(el.text.strip())
    return urls


def filter_directory_like(urls: list[str]) -> list[str]:
    return [u for u in urls if DIRECTORY_SLUG_RE.search(u)]


def parse_robots_sitemaps(robots_txt: str) -> list[str]:
    return [
        line.split(":", 1)[1].strip()
        for line in robots_txt.splitlines()
        if line.strip().lower().startswith("sitemap:")
    ]


# ---------------------------------------------------------------------------
# 2. CMS fingerprint -> known directory routes
# ---------------------------------------------------------------------------

CMS_FINGERPRINTS: list[tuple[str, re.Pattern, list[str]]] = [
    ("finalsite", re.compile(r"finalsite|fs-sticky-header|cdn\.finalsite", re.I),
     ["/fs/directory", "/our-school/faculty-staff"]),
    ("blackboard", re.compile(r"blackboard|schoolwires|/cms/lib|/domain/\d+", re.I),
     ["/Page/1", "/domain/1", "/site/Default.aspx?PageID="]),
    ("edlio", re.compile(r"edlio|/apps/pages/|/apps/staff/", re.I),
     ["/apps/staff/", "/apps/pages/"]),
    ("apptegy", re.compile(r"apptegy|thrillshare|/o/[\w-]+/", re.I),
     ["/staff", "/page/staff-directory"]),
    ("schoolmessenger", re.compile(r"schoolmessenger|campussuite", re.I),
     ["/staff_directory", "/departments"]),
    ("wordpress", re.compile(r"wp-content|wp-json|/wp-includes/", re.I),
     ["/staff/", "/faculty/", "/our-team/", "/?page_id=165"]),
]


def detect_cms(html_text: str) -> str | None:
    for name, pattern, _paths in CMS_FINGERPRINTS:
        if pattern.search(html_text or ""):
            return name
    return None


def cms_candidate_paths(cms_name: str) -> list[str]:
    for name, _pattern, paths in CMS_FINGERPRINTS:
        if name == cms_name:
            return list(paths)
    return []


# ---------------------------------------------------------------------------
# 3. Nav / footer link crawl
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")


def extract_nav_links(html_text: str, base_url: str) -> list[tuple[str, str]]:
    """Return (absolute_url, visible_text) for every <a> tag whose link text
    looks like a directory/staff/faculty/department link."""
    out: list[tuple[str, str]] = []
    for href, inner in _LINK_RE.findall(html_text or ""):
        text = _TAG_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if not text or not DIRECTORY_LINK_TEXT_RE.search(text):
            continue
        if href.startswith("#") or href.lower().startswith("javascript:"):
            continue
        out.append((resolve_href(href, base_url), text))
    return out


def resolve_href(href: str, base_url: str) -> str:
    if href.startswith("http"):
        return href
    return urllib.parse.urljoin(base_url, href)


def same_domain(url: str, base_url: str) -> bool:
    try:
        a = urllib.parse.urlparse(url).netloc.lower().lstrip("www.")
        b = urllib.parse.urlparse(base_url).netloc.lower().lstrip("www.")
        return bool(a) and a == b
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 5. Internal search box detection
# ---------------------------------------------------------------------------

_SEARCH_FORM_RE = re.compile(
    r'<form\b[^>]*>(?:(?!</form>).)*?(?:type=["\']search["\']|name=["\']q["\']|role=["\']search["\'])',
    re.IGNORECASE | re.DOTALL,
)
_FORM_ACTION_RE = re.compile(r'<form\b[^>]*\baction=["\']([^"\']*)["\']', re.IGNORECASE)


def has_internal_search(html_text: str) -> bool:
    return bool(_SEARCH_FORM_RE.search(html_text or ""))


def internal_search_url(html_text: str, base_url: str, query: str = "staff") -> str | None:
    if not has_internal_search(html_text):
        return None
    m = _FORM_ACTION_RE.search(html_text)
    action = resolve_href(m.group(1), base_url) if m and m.group(1) else base_url
    sep = "&" if "?" in action else "?"
    return f"{action}{sep}q={urllib.parse.quote(query)}"


# ---------------------------------------------------------------------------
# 4. Site-scoped search engine query — needs an external search API.
# ---------------------------------------------------------------------------

def build_site_search_queries(domain: str) -> list[str]:
    """Query strings for a site-scoped search. `discover()` only issues
    these if a `search_fn` callable is supplied by the caller (no search API
    key is bundled with this project) — see its docstring.
    """
    return [
        f"site:{domain} (staff OR faculty OR directory)",
        f"site:{domain} art teacher",
    ]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def discover(
    base_url: str,
    session,
    robots,
    search_fn=None,
    timeout: float = 8.0,
) -> DiscoveryResult:
    """Run all six discovery sources and return a ranked candidate list plus
    a full attempt log. `session` is an aiohttp.ClientSession, `robots` a
    RobotsCache (see src.scraper). `search_fn(query) -> list[str]` is an
    optional site-scoped search callable — without one, source 4 is skipped
    and logged as such (never silently treated as "tried").
    """
    import aiohttp

    result = DiscoveryResult()
    seen_urls: set[str] = set()

    def add_candidate(url: str, source: str, priority: int) -> None:
        if url not in seen_urls:
            seen_urls.add(url)
            result.candidates.append(PathCandidate(url=url, source=source, priority=priority))

    async def fetch(url: str) -> tuple[int | None, str]:
        if not await robots.can_fetch(url, session):
            result.attempts.append(PathAttempt(url=url, status=None, rejected_because="robots.txt disallow"))
            return None, ""
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                text = await resp.text(errors="replace") if resp.status < 400 else ""
                if resp.status >= 400:
                    result.attempts.append(
                        PathAttempt(url=url, status=resp.status, rejected_because=f"HTTP {resp.status}")
                    )
                return resp.status, text
        except Exception as exc:
            result.attempts.append(PathAttempt(url=url, status=None, rejected_because=f"fetch error: {exc}"))
            return None, ""

    # --- 1. sitemaps ---
    robots_status, robots_text = await fetch(base_url.rstrip("/") + "/robots.txt")
    sitemap_urls = parse_robots_sitemaps(robots_text) if robots_text else []
    for candidate_sitemap in sitemap_urls + [
        base_url.rstrip("/") + "/sitemap.xml",
        base_url.rstrip("/") + "/sitemap_index.xml",
    ]:
        status, text = await fetch(candidate_sitemap)
        if status and status < 400 and text:
            urls = parse_sitemap_urls(text)
            hits = filter_directory_like(urls)
            if not hits:
                result.attempts.append(
                    PathAttempt(url=candidate_sitemap, status=status, rejected_because="no directory-like slugs in sitemap")
                )
            for u in hits:
                add_candidate(u, "sitemap", priority=1)

    # --- 2. CMS fingerprint ---
    home_status, home_text = await fetch(base_url)
    cms = detect_cms(home_text) if home_text else None
    if cms:
        for path in cms_candidate_paths(cms):
            add_candidate(base_url.rstrip("/") + path, f"cms:{cms}", priority=2)
    elif home_text:
        result.attempts.append(
            PathAttempt(url=base_url, status=home_status, rejected_because="no known CMS fingerprint matched")
        )

    # --- 3. nav + footer crawl (depth 1 here; deeper levels are the
    #        caller's job via a real browser since menus are often JS-driven) ---
    if home_text:
        for url, _text in extract_nav_links(home_text, base_url):
            if same_domain(url, base_url):
                add_candidate(url, "nav", priority=3)
        if not any(c.source == "nav" for c in result.candidates):
            result.attempts.append(
                PathAttempt(url=base_url, status=home_status, rejected_because="no directory-like nav links found")
            )

    # --- 4. site-scoped search ---
    if search_fn:
        for query in build_site_search_queries(urllib.parse.urlparse(base_url).netloc):
            hits = await search_fn(query) if _is_awaitable(search_fn) else search_fn(query)
            for u in hits or []:
                if same_domain(u, base_url):
                    add_candidate(u, "site_search", priority=4)
    else:
        result.attempts.append(
            PathAttempt(url=base_url, status=None, rejected_because="site-scoped search skipped: no search_fn/API key configured")
        )

    # --- 5. internal search box ---
    if home_text:
        search_url = internal_search_url(home_text, base_url)
        if search_url:
            add_candidate(search_url, "internal_search", priority=5)
        else:
            result.attempts.append(
                PathAttempt(url=base_url, status=home_status, rejected_because="no internal search form found")
            )

    # --- 6. department pages ---
    for path in DEPARTMENT_PATHS:
        add_candidate(base_url.rstrip("/") + path, "department", priority=6)

    result.candidates.sort(key=lambda c: c.priority)
    return result


def _is_awaitable(fn) -> bool:
    import inspect
    return inspect.iscoroutinefunction(fn)


async def probe_candidates(
    session,
    candidates: list[PathCandidate],
    robots,
    match_fn,
    timeout: float = 6.0,
) -> tuple[PathCandidate | None, list[PathAttempt]]:
    """Try candidates in priority order (not concurrently — order matters
    here since it's a ranked list) and return the first whose body satisfies
    match_fn, plus the attempt log for everything tried."""
    import aiohttp

    attempts: list[PathAttempt] = []
    for cand in candidates:
        if not await robots.can_fetch(cand.url, session):
            attempts.append(PathAttempt(url=cand.url, status=None, rejected_because="robots.txt disallow"))
            continue
        try:
            async with session.get(
                cand.url, timeout=aiohttp.ClientTimeout(total=timeout), allow_redirects=True
            ) as resp:
                if resp.status >= 400:
                    attempts.append(PathAttempt(url=cand.url, status=resp.status, rejected_because=f"HTTP {resp.status}"))
                    continue
                text = await resp.text(errors="replace")
                if match_fn(text):
                    attempts.append(PathAttempt(url=cand.url, status=resp.status, rejected_because="MATCHED"))
                    return cand, attempts
                attempts.append(
                    PathAttempt(url=cand.url, status=resp.status, rejected_because="200 OK but no staff/faculty content matched")
                )
        except Exception as exc:
            attempts.append(PathAttempt(url=cand.url, status=None, rejected_because=f"fetch error: {exc}"))
    return None, attempts
