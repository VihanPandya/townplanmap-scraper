"""Polite HTTP access: robots.txt, rate limiting, retries and an on-disk cache."""

from __future__ import annotations

import hashlib
import logging
import random
import re
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger("tpmap.net")

DEFAULT_UA = (
    "tpmap-scraper/1.0 (+https://github.com/vihanpandya/townplanmap-scraper) "
    "python-requests"
)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def normalise_url(url: str) -> str:
    """Drop fragments and collapse an empty path to '/' so URLs dedupe cleanly."""
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc, p.path or "/", p.params, p.query, ""))


def slugify(value: str, maxlen: int = 120) -> str:
    """Filesystem-safe slug, stable across runs."""
    value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    value = re.sub(r"[^\w\s.-]", "-", value).strip("-_ ")
    value = re.sub(r"[-\s]+", "-", value)
    return (value[:maxlen].strip("-_") or "unnamed").lower()


def url_to_stem(url: str) -> str:
    """A readable, collision-resistant filename stem for a URL."""
    p = urlparse(url)
    tail = (p.path.rstrip("/").rsplit("/", 1)[-1] or p.netloc)
    stem = slugify(tail, 90)
    digest = hashlib.sha1(url.encode()).hexdigest()[:8]
    return f"{stem}-{digest}"


class Fetcher:
    """A rate-limited requests session that respects robots.txt."""

    def __init__(self, rate: float = 1.0, timeout: int = 30, retries: int = 4,
                 user_agent: str = DEFAULT_UA, obey_robots: bool = True,
                 cache_dir: str | Path | None = None):
        self.min_interval = 1.0 / rate if rate > 0 else 0.0
        self.timeout = timeout
        self.retries = retries
        self.user_agent = user_agent
        self.obey_robots = obey_robots
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}

    # -- robots -----------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        host = urlparse(url).netloc
        if host in self._robots:
            return self._robots[host]

        rp = RobotFileParser()
        robots_url = urljoin(f"{urlparse(url).scheme}://{host}", "/robots.txt")
        try:
            resp = self.session.get(robots_url, timeout=self.timeout)
            if resp.status_code >= 400:
                # No usable robots.txt means nothing is disallowed.
                rp = None
            else:
                rp.parse(resp.text.splitlines())
        except requests.RequestException as exc:
            log.debug("robots.txt fetch failed for %s: %s", host, exc)
            rp = None
        self._robots[host] = rp
        return rp

    def allowed(self, url: str) -> bool:
        if not self.obey_robots:
            return True
        rp = self._robots_for(url)
        if rp is None:
            return True
        return rp.can_fetch(self.user_agent, url)

    def crawl_delay(self, url: str) -> float:
        rp = self._robots_for(url) if self.obey_robots else None
        if rp is None:
            return 0.0
        try:
            delay = rp.crawl_delay(self.user_agent)
        except Exception:
            delay = None
        return float(delay) if delay else 0.0

    # -- throttling -------------------------------------------------------

    def _wait(self, url: str) -> None:
        host = urlparse(url).netloc
        interval = max(self.min_interval, self.crawl_delay(url))
        if interval <= 0:
            return
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        gap = interval - elapsed
        if gap > 0:
            time.sleep(gap + random.uniform(0, 0.25 * interval))
        self._last_hit[host] = time.monotonic()

    # -- cache ------------------------------------------------------------

    def _cache_path(self, url: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".bin")

    # -- fetching ---------------------------------------------------------

    def get(self, url: str, **kwargs) -> requests.Response:
        """GET with retry/backoff.  Raises requests.RequestException on failure."""
        if not self.allowed(url):
            raise PermissionError(f"robots.txt disallows {url}")

        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            self._wait(url)
            try:
                resp = self.session.get(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                log.debug("attempt %d for %s failed: %s", attempt + 1, url, exc)
            else:
                if resp.status_code not in RETRY_STATUS:
                    return resp
                last_exc = requests.HTTPError(f"HTTP {resp.status_code} for {url}",
                                              response=resp)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(float(retry_after), 60.0))
                    continue

            if attempt < self.retries:
                time.sleep(min(2.0 ** attempt + random.uniform(0, 0.5), 30.0))

        raise last_exc if last_exc else requests.RequestException(f"failed: {url}")

    def get_bytes(self, url: str, *, use_cache: bool = True) -> bytes:
        """Fetch a binary body, going through the on-disk cache when enabled."""
        path = self._cache_path(url)
        if use_cache and path and path.exists():
            log.debug("cache hit %s", url)
            return path.read_bytes()

        resp = self.get(url)
        resp.raise_for_status()
        blob = resp.content
        if path:
            path.write_bytes(blob)
        return blob

    def get_text(self, url: str, *, use_cache: bool = True) -> str:
        blob = self.get_bytes(url, use_cache=use_cache)
        return blob.decode("utf-8", errors="replace")

    def close(self) -> None:
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
