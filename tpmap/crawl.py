"""Finding the pages worth scraping.

Two strategies, tried in order: the site's own sitemap (cheap and complete when
it exists), then a bounded breadth-first link walk (works regardless).
"""

from __future__ import annotations

import gzip
import logging
import re
import xml.etree.ElementTree as ET
from collections import deque
from urllib.parse import urljoin, urlparse

from .net import normalise_url

log = logging.getLogger("tpmap.crawl")

SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SITEMAP_CANDIDATES = ["/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml",
                      "/sitemap/sitemap.xml"]

# Path prefixes that hold an actual map on TownPlanMap.
MAP_PATH_RE = re.compile(r"^/(tp|dp|suda|auda|scheme|map|project)s?(/|$)", re.I)
SKIP_PATH_RE = re.compile(
    r"^/(login|signup|register|account|cart|checkout|privacy|terms|contact|about|blog|api/auth)\b",
    re.I)


def _parse_sitemap(text: str) -> tuple[list[str], list[str]]:
    """Return (page urls, nested sitemap urls) from one sitemap document."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        log.debug("sitemap parse error: %s", exc)
        return [], []

    tag = root.tag.split("}")[-1]
    locs = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]
    if tag == "sitemapindex":
        return [], locs
    return locs, []


def _noop(_message: str) -> None:
    pass


def sitemap_urls(fetcher, base_url: str, max_sitemaps: int = 60,
                 progress=_noop) -> list[str]:
    """Walk the sitemap tree (including indexes and .gz) and return page URLs."""
    origin = "{0.scheme}://{0.netloc}".format(urlparse(base_url))
    queue = deque(urljoin(origin, p) for p in SITEMAP_CANDIDATES)
    # Guesses only: once a real sitemap is found there is no point probing the
    # rest, and each miss costs a request plus its retries.
    guesses = {normalise_url(urljoin(origin, p)) for p in SITEMAP_CANDIDATES}

    # robots.txt often points at the real sitemap.
    progress(f"reading {urljoin(origin, '/robots.txt')}")
    try:
        robots = fetcher.get_text(urljoin(origin, "/robots.txt"))
        for line in robots.splitlines():
            if line.lower().startswith("sitemap:"):
                queue.append(line.split(":", 1)[1].strip())
    except Exception as exc:
        progress(f"  no robots.txt ({type(exc).__name__})")
        log.debug("robots.txt read failed: %s", exc)

    seen_maps: set[str] = set()
    pages: list[str] = []

    while queue and len(seen_maps) < max_sitemaps:
        sm = normalise_url(queue.popleft())
        if sm in seen_maps:
            continue
        seen_maps.add(sm)
        progress(f"trying {sm}")
        try:
            blob = fetcher.get_bytes(sm)
        except Exception as exc:
            progress(f"  not there ({type(exc).__name__})")
            log.debug("sitemap %s unavailable: %s", sm, exc)
            continue
        if blob[:2] == b"\x1f\x8b":
            try:
                blob = gzip.decompress(blob)
            except OSError:
                continue
        text = blob.decode("utf-8", errors="replace")
        if "<" not in text:
            continue
        found, nested = _parse_sitemap(text)
        pages.extend(found)
        queue.extend(nested)
        if found:
            queue = deque(u for u in queue if normalise_url(u) not in guesses)
        if found or nested:
            progress(f"  {len(found)} url(s), {len(nested)} nested sitemap(s)")
            log.info("sitemap %s -> %d urls, %d nested", sm, len(found), len(nested))

    return list(dict.fromkeys(normalise_url(u) for u in pages))


def crawl_links(fetcher, base_url: str, max_pages: int = 300,
                same_host_only: bool = True, progress=_noop) -> list[str]:
    """Breadth-first walk of the site collecting internal page URLs."""
    from bs4 import BeautifulSoup

    origin_host = urlparse(base_url).netloc
    seen = {normalise_url(base_url)}
    queue = deque([normalise_url(base_url)])
    found: list[str] = []

    while queue and len(found) < max_pages:
        url = queue.popleft()
        try:
            resp = fetcher.get(url)
        except Exception as exc:
            log.debug("crawl skip %s: %s", url, exc)
            continue
        if "html" not in resp.headers.get("content-type", ""):
            continue
        found.append(url)
        progress(f"  [{len(found)}/{max_pages}] {url}")

        soup = BeautifulSoup(resp.text, "lxml")
        for a in soup.find_all("a", href=True):
            link = normalise_url(urljoin(url, a["href"]))
            parsed = urlparse(link)
            if parsed.scheme not in ("http", "https"):
                continue
            if same_host_only and parsed.netloc != origin_host:
                continue
            if SKIP_PATH_RE.match(parsed.path) or link in seen:
                continue
            seen.add(link)
            queue.append(link)

    return found


def looks_like_map_page(url: str) -> bool:
    return bool(MAP_PATH_RE.match(urlparse(url).path))


def discover_pages(fetcher, base_url: str, *, max_pages: int = 300,
                   map_pages_only: bool = True, progress=_noop) -> list[str]:
    """Sitemap first, link crawl as a fallback."""
    progress("looking for a sitemap...")
    urls = sitemap_urls(fetcher, base_url, progress=progress)
    if urls:
        progress(f"sitemap yielded {len(urls)} url(s)")
        log.info("sitemap yielded %d urls", len(urls))
    else:
        # The crawl is rate-limited, so without a running commentary this looks
        # like a hang -- it can legitimately take minutes.
        progress(f"no sitemap. Crawling links instead (up to {max_pages} pages at "
                 f"~1/second -- this can take a few minutes; Ctrl-C to stop)")
        urls = crawl_links(fetcher, base_url, max_pages=max_pages, progress=progress)

    if map_pages_only:
        maps = [u for u in urls if looks_like_map_page(u)]
        if maps:
            progress(f"{len(maps)} of them look like map pages")
            return maps
        progress(f"none of the {len(urls)} URLs match a map-page pattern; "
                 f"showing everything found")
        log.warning("no URLs matched the known map-page patterns; "
                    "returning all %d discovered pages", len(urls))
    return urls
