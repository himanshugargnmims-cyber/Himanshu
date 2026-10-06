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
DETAIL_RE = re.compile(r"^/communities/[\w.\-]+/?$", re.I)
ADDRESS_RE = re.compile(r"^(?P<street>.+?),\s*(?P<city>[^,\d]+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$")
STREET_OK_RE = re.compile(r"^(\d|p\.?\s*o\.?\s*box|post office box)", re.I)
MAX_DIRECTORY_PAGES = 50  # guard against a pager that never ends


def _url(path):
    return urljoin(config.BASE_URL + "/", path.lstrip("/"))


def _get(session, url):
    resp = session.get(url if url.startswith("http") else _url(url), timeout=30)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def _detail_links(soup, page_url):
    links = set()
    for a in soup.find_all("a", href=True):
        path = urlparse(urljoin(page_url, a["href"])).path
        if DETAIL_RE.match(path):
            links.add(path.rstrip("/"))
    return links


def _next_page(soup, page_url):
    link = soup.find("a", rel="next") or next(
        (a for a in soup.select(".pager a[href]") if "next" in a.get_text().lower() or a.get_text().strip() in ("»", "›")), None)
    return urljoin(page_url, link["href"]) if link else None


def discover(session):
    """Return ({detail_path: [where found]}, {page: soup}, directory_claim) across directory and other pages."""
    found = {}

    def add(paths, source):
        for p in paths:
            found.setdefault(p, []).append(source)

    url, seen, directory_claim = _url("/communities"), set(), None
    while url and url not in seen and len(seen) < MAX_DIRECTORY_PAGES:
        seen.add(url)
        soup = _get(session, url)
        add(_detail_links(soup, url), "directory")
        m = re.search(r"(\d+)\s+communities listed", soup.get_text(" ", strip=True), re.I)
        directory_claim = directory_claim or (int(m.group(1)) if m else None)
        url = _next_page(soup, url)

    pages = {}
    for page in DISCOVERY_PAGES:
        pages[page] = _get(session, page)
        add(_detail_links(pages[page], _url(page)), f"page:{page}")
    return found, pages, directory_claim


def parse_detail(soup, path):
    fields = {}
    dl = soup.select_one("dl.detail")
    if dl is None:
        raise ValueError(f"{path}: no <dl class=detail>")
    for dt in dl.find_all("dt"):
        fields[dt.get_text(strip=True).lower()] = dt.find_next_sibling("dd")

    # Works for '210 Orchard Lane<br>Maplewood, OH 44280' and for a single line with commas.
    address_text = ", ".join(s.strip().rstrip(",") for s in fields["address"].stripped_strings)
    m = ADDRESS_RE.match(address_text)
    if not m or not STREET_OK_RE.match(m["street"]):
        raise ValueError(f"{path}: cannot parse address {address_text!r}")
    offerings = [b.get_text(strip=True) for b in fields["care offerings"].select(".badge")] if "care offerings" in fields else []
    text = lambda k: fields[k].get_text(" ", strip=True) if k in fields else ""
    h1 = soup.find("h1")
    name = " ".join(t.strip() for t in h1.find_all(string=True, recursive=False) if t.strip()) or h1.get_text(" ", strip=True)
    return {
        "name": name,
        "address": m["street"],
        "city": m["city"].strip(),
        "state": m["state"],
        "zip": m["zip"],
        "care_offerings": offerings,
        "phone": text("phone"),
        "administrator": text("administrator"),
        "url": _url(path),
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
    found, pages, directory_claim = discover(session)
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
        "directory_claim": directory_claim,
    }


def scrape(session=None):
    return scrape_site(session)["locations"]


if __name__ == "__main__":
    import json
    import sys

    site = scrape_site()
    print(json.dumps(site, indent=2))
    print(f"{len(site['locations'])} locations; homepage claims {site['claimed_count']}", file=sys.stderr)
