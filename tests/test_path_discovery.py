from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.path_discovery import (
    parse_sitemap_urls,
    filter_directory_like,
    parse_robots_sitemaps,
    detect_cms,
    cms_candidate_paths,
    extract_nav_links,
    has_internal_search,
    internal_search_url,
)


def test_parse_sitemap_urls():
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://example.org/about</loc></url>
        <url><loc>https://example.org/staff-directory</loc></url>
    </urlset>"""
    urls = parse_sitemap_urls(xml)
    assert "https://example.org/staff-directory" in urls
    assert len(urls) == 2


def test_filter_directory_like():
    urls = ["https://x.org/about", "https://x.org/faculty", "https://x.org/lunch-menu"]
    assert filter_directory_like(urls) == ["https://x.org/faculty"]


def test_parse_robots_sitemaps():
    robots_txt = "User-agent: *\nDisallow: /admin\nSitemap: https://x.org/sitemap.xml\n"
    assert parse_robots_sitemaps(robots_txt) == ["https://x.org/sitemap.xml"]


def test_detect_cms_wordpress():
    html = '<link rel="stylesheet" href="/wp-content/themes/main/style.css">'
    assert detect_cms(html) == "wordpress"
    assert "/?page_id=165" in cms_candidate_paths("wordpress")


def test_detect_cms_blackboard():
    html = '<a href="/domain/42">Staff</a>'
    assert detect_cms(html) == "blackboard"


def test_detect_cms_none():
    assert detect_cms("<html><body>hello</body></html>") is None


def test_extract_nav_links_matches_staff_directory_text():
    html = """
    <nav>
      <a href="/staff-directory">Staff Directory</a>
      <a href="/lunch-menu">Lunch Menu</a>
      <a href="/fine-arts">Fine Arts</a>
    </nav>
    """
    links = extract_nav_links(html, "https://school.org")
    urls = {u for u, _t in links}
    assert "https://school.org/staff-directory" in urls
    assert "https://school.org/fine-arts" in urls
    assert "https://school.org/lunch-menu" not in urls


def test_internal_search_detection():
    html = '<form action="/search" role="search"><input type="text" name="q"></form>'
    assert has_internal_search(html) is True
    url = internal_search_url(html, "https://school.org", query="staff")
    assert url == "https://school.org/search?q=staff"


def test_no_internal_search():
    html = "<div>no forms here</div>"
    assert has_internal_search(html) is False
    assert internal_search_url(html, "https://school.org") is None
