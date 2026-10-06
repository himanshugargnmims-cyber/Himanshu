"""Scrape every Bellhaven community from the public website.

Discovery is deliberately broader than the directory: the homepage announced a
new community ("Bellhaven Meadows of Findlay") that the paginated directory did
not list (homepage says 35 communities, directory says 34). So we collect
/communities/<slug> links from the directory pages AND the other site pages,
then read every detail page for the address and care offerings.
"""
import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from . import config

DISCOVERY_PAGES = ["/", "/about"]
DETAIL_RE = re.compile(r"^/communities/[a-z0-9-]+/?$")
CITY_LINE_RE = re.compile(r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
MAX_DIRECTORY_PAGES = 50  # guard against a pager that never ends


def _get(session, path):
    resp = session.get(urljoin(config.BASE_URL + "/", path.lstrip("/")), timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _detail_links(soup):
    links = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(a["href"]).path
        if DETAIL_RE.match(path):
            links.add(path.rstrip("/"))
    return links


def discover(session):
    """Return ({detail_path: [where found]}, {page: soup}) across directory and other pages."""
    found = {}

    def add(paths, source):
        for p in paths:
            found.setdefault(p, []).append(source)

    path, seen_pages = "/communities", set()
    while path and path not in seen_pages and len(seen_pages) < MAX_DIRECTORY_PAGES:
        seen_pages.add(path)
        soup = _get(session, path)
        add(_detail_links(soup), "directory")
        nxt = next((a for a in soup.select(".pager a[href]") if "next" in a.get_text().lower()), None)
        path = nxt["href"] if nxt else None

    pages = {}
    for page in DISCOVERY_PAGES:
        pages[page] = _get(session, page)
        add(_detail_links(pages[page]), f"page:{page}")
    return found, pages


def parse_detail(soup, path):
    fields = {}
    dl = soup.select_one("dl.detail")
    if dl is None:
        raise ValueError(f"{path}: no <dl class=detail>")
    for dt in dl.find_all("dt"):
        dd = dt.find_next_sibling("dd")
        fields[dt.get_text(strip=True).lower()] = dd

    addr_lines = [s.strip() for s in fields["address"].stripped_strings]
    m = CITY_LINE_RE.match(addr_lines[-1])
    if not m:
        raise ValueError(f"{path}: cannot parse city/state/zip from {addr_lines!r}")
    offerings = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")] if "care offerings" in fields else []
    text = lambda k: fields[k].get_text(" ", strip=True) if k in fields else ""
    return {
        "name": soup.find("h1").get_text(" ", strip=True),
        "address": " ".join(addr_lines[:-1]),
        "city": m["city"],
        "state": m["state"],
        "zip": m["zip"],
        "care_offerings": offerings,
        "phone": text("phone"),
        "administrator": text("administrator"),
        "url": config.BASE_URL + path,
        "slug": path.rsplit("/", 1)[-1],
    }


def _claimed_count(pages):
    """'Today we proudly serve 35 communities' -> 35 (a cross-check on completeness)."""
    for soup in pages.values():
        m = re.search(r"serve\s+(\d+)\s+communities", soup.get_text(" ", strip=True), re.I)
        if m:
            return int(m.group(1))
    return None


def scrape_site(session=None):
    session = session or requests.Session()
    found, pages = discover(session)
    locations = []
    for path in sorted(found):
        loc = parse_detail(_get(session, path), path)
        loc["found_on"] = sorted(set(found[path]))
        locations.append(loc)
    about = pages.get("/about")
    return {
        "locations": locations,
        "about_text": about.select_one(".wrap").get_text(" ", strip=True) if about and about.select_one(".wrap") else "",
        "claimed_count": _claimed_count(pages),
    }


def scrape(session=None):
    return scrape_site(session)["locations"]


if __name__ == "__main__":
    import json
    import sys

    site = scrape_site()
    print(json.dumps(site, indent=2))
    print(f"{len(site['locations'])} locations; homepage claims {site['claimed_count']}", file=sys.stderr)
