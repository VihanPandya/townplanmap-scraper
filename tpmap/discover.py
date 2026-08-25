"""Endpoint discovery.

TownPlanMap is a JavaScript map application, so the geodata is not sitting in
the served HTML.  Rather than hard-coding selectors that would rot the moment
the front-end changes, this module drives a real browser and watches what the
page itself does, across four independent channels:

  network     every response the page fetches, classified by URL/content-type
  init-hook   constructors patched before page scripts run (google.maps.KmlLayer
              hands its URL to Google's servers, so that fetch never appears in
              the browser's own network log -- the hook is the only way to see it)
  source-scan regex over HTML and JS bodies for literal .kml/.kmz references
  page-probe  live map objects interrogated after load (Leaflet / Mapbox GL /
              Google Data layers already hold parsed GeoJSON in memory)

The union of those four is reported, so whatever shape the site takes we come
away with something to download.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field, asdict
from urllib.parse import urljoin, urlparse

log = logging.getLogger("tpmap.discover")

# Direct geodata file extensions.
GEO_EXT_RE = re.compile(r"\.(kmz|kml|geojson|topojson)\b", re.I)
# Literal KML/KMZ URLs hiding in HTML or JS source.
SOURCE_KML_RE = re.compile(
    r"""["'(]\s*((?:https?:)?//[^"'()\s]+?\.km[lz](?:\?[^"'()\s]*)?)\s*["')]""", re.I)
SOURCE_REL_KML_RE = re.compile(
    r"""["'(]\s*(/[^"'()\s]*?\.km[lz](?:\?[^"'()\s]*)?)\s*["')]""", re.I)
# Common spatial service shapes.
ARCGIS_RE = re.compile(r"/(FeatureServer|MapServer|ImageServer)(/|\?|$)", re.I)
OGC_RE = re.compile(r"[?&]service=(wfs|wms)\b", re.I)
TILE_RE = re.compile(r"\.(pbf|mvt)\b|\{z\}|/\d+/\d+/\d+\.(pbf|mvt|png|jpg)", re.I)

JSON_CT_RE = re.compile(r"json", re.I)
SKIP_CT_RE = re.compile(r"^(image|font|video|audio)/|text/css", re.I)
SKIP_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|woff2?|ttf|eot|css|mp4)\b", re.I)

# Bodies larger than this are classified but not held in memory.
MAX_CAPTURE = 32 * 1024 * 1024

# Headless Chrome advertises itself in its UA, and plenty of sites serve those
# requests a stub page. Present as ordinary desktop Chrome instead.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Signatures of an interstitial served instead of the real page.
CHALLENGE_RE = re.compile(
    r"(just a moment|checking your browser|attention required|cf-browser-verification|"
    r"enable javascript and cookies|access denied|are you a robot|captcha|"
    r"unusual traffic)", re.I)


@dataclass
class Hit:
    """One discovered geodata endpoint."""
    url: str
    kind: str                 # kml | kmz | geojson | esri | embedded | arcgis-service |
                              # ogc-service | tiles
    source: str               # network | init-hook | source-scan | page-probe |
                              # inline-json
    content_type: str = ""
    size: int = 0
    note: str = ""

    def key(self) -> tuple:
        return (self.url, self.kind)


@dataclass
class Report:
    page_url: str
    hits: list[Hit] = field(default_factory=list)
    inline: list[dict] = field(default_factory=list)   # GeoJSON lifted straight from the page
    errors: list[str] = field(default_factory=list)
    # Everything the page loaded, kept so a run that finds nothing can still be
    # diagnosed from the report alone.
    responses: list[dict] = field(default_factory=list)

    def add(self, hit: Hit) -> None:
        if all(hit.key() != h.key() for h in self.hits):
            self.hits.append(hit)

    def by_kind(self, *kinds: str) -> list[Hit]:
        return [h for h in self.hits if h.kind in kinds]

    def downloadable(self) -> list[Hit]:
        """Hits we can turn into KML by a plain GET, best format first.

        Inline-JSON hits are markers for the report: their content came from the
        page body and is already in ``inline``, and their pseudo-URL points at
        the HTML page, not at anything fetchable.
        """
        order = {"kml": 0, "kmz": 1, "geojson": 2, "esri": 3, "embedded": 4}
        return sorted((h for h in self.hits
                       if h.kind in order and h.source != "inline-json"),
                      key=lambda h: order[h.kind])

    def to_dict(self) -> dict:
        return {
            "page_url": self.page_url,
            "hits": [asdict(h) for h in self.hits],
            "inline_layers": len(self.inline),
            "inline_features": sum(len(l.get("features", [])) for l in self.inline),
            "errors": self.errors,
            "responses_seen": len(self.responses),
            "responses": self.responses[:200],
        }


def classify(url: str, content_type: str = "", body: bytes | None = None) -> str | None:
    """Best guess at what a response holds, or None if it is not geodata."""
    path = urlparse(url).path
    if SKIP_EXT_RE.search(path) and not GEO_EXT_RE.search(path):
        return None
    if content_type and SKIP_CT_RE.search(content_type):
        return None

    ext = GEO_EXT_RE.search(path)
    if ext:
        found = ext.group(1).lower()
        return {"kml": "kml", "kmz": "kmz",
                "geojson": "geojson", "topojson": "geojson"}[found]

    ct = content_type.lower()
    if "vnd.google-earth.kmz" in ct:
        return "kmz"
    if "vnd.google-earth.kml" in ct:
        return "kml"

    if body:
        head = body[:2048].lstrip()
        if head.startswith(b"<?xml") and b"<kml" in body[:8192]:
            return "kml"
        if body[:2] == b"PK" and GEO_EXT_RE.search(path):
            return "kmz"

    # JSON payloads need a peek inside to tell geodata from ordinary API noise.
    if JSON_CT_RE.search(ct) or path.endswith(".json"):
        if body:
            try:
                obj = json.loads(body.decode("utf-8", errors="replace"))
            except (ValueError, UnicodeDecodeError):
                obj = None
            if obj is not None:
                from .kml import looks_like_esri, looks_like_geojson
                if looks_like_geojson(obj):
                    return "geojson"
                if looks_like_esri(obj):
                    return "esri"
                # Not GeoJSON at the top level, but the geometry is usually
                # wrapped in an API envelope or written in some other spelling.
                from .coerce import contains_geodata
                if contains_geodata(obj):
                    return "embedded"
                return None

    if ARCGIS_RE.search(url):
        return "arcgis-service"
    if OGC_RE.search(url):
        return "ogc-service"
    if TILE_RE.search(url):
        return "tiles"
    return None


def scan_source(text: str, base_url: str) -> list[str]:
    """Pull literal .kml/.kmz URLs out of HTML or JS source."""
    found = []
    for match in SOURCE_KML_RE.finditer(text):
        url = match.group(1)
        if url.startswith("//"):
            url = urlparse(base_url).scheme + ":" + url
        found.append(url)
    for match in SOURCE_REL_KML_RE.finditer(text):
        found.append(urljoin(base_url, match.group(1)))
    # preserve order, drop dupes
    return list(dict.fromkeys(found))


# --------------------------------------------------------------------------
# browser-side instrumentation
# --------------------------------------------------------------------------

# Installed before any page script runs.  google.maps.KmlLayer sends its URL to
# Google's tile servers rather than fetching it in-page, so wrapping the
# constructor is the only way to observe it.
INIT_SCRIPT = r"""
(() => {
  window.__tpmap = { kmlUrls: [], dataUrls: [], patched: false };
  const note = (bucket, u) => {
    try { if (u && window.__tpmap[bucket].indexOf(u) === -1) window.__tpmap[bucket].push(String(u)); }
    catch (e) {}
  };

  const patchGoogle = () => {
    if (window.__tpmap.patched) return true;
    const g = window.google && window.google.maps;
    if (!g) return false;
    if (g.KmlLayer) {
      const Orig = g.KmlLayer;
      function KmlLayer(opts) {
        if (opts) note('kmlUrls', opts.url || opts);
        return new Orig(...arguments);
      }
      KmlLayer.prototype = Orig.prototype;
      Object.setPrototypeOf(KmlLayer, Orig);
      g.KmlLayer = KmlLayer;
    }
    if (g.Data && g.Data.prototype && g.Data.prototype.loadGeoJson) {
      const orig = g.Data.prototype.loadGeoJson;
      g.Data.prototype.loadGeoJson = function (url) { note('dataUrls', url); return orig.apply(this, arguments); };
    }
    window.__tpmap.patched = true;
    return true;
  };

  if (!patchGoogle()) {
    const timer = setInterval(() => { if (patchGoogle()) clearInterval(timer); }, 50);
    setTimeout(() => clearInterval(timer), 30000);
  }
})();
"""

# Run after load: interrogate whatever mapping library is actually in use.
#
# Every property access here is guarded. Touching any property of a cross-origin
# iframe's Window throws SecurityError, and one uncaught throw would abort the
# whole probe and lose the channel -- so `safe()` wraps all of it.
PROBE_SCRIPT = r"""
() => {
  const result = { layers: [], urls: [], libs: [] };
  const safe = (obj, prop) => { try { return obj[prop]; } catch (e) { return undefined; } };
  const callSafe = (obj, method, ...args) => {
    try {
      const fn = obj[method];
      if (typeof fn !== 'function') return undefined;
      return fn.apply(obj, args);
    } catch (e) { return undefined; }
  };
  const push = (gj) => {
    try {
      if (!gj || typeof gj !== 'object') return;
      if (gj.type === 'FeatureCollection' && (!gj.features || !gj.features.length)) return;
      result.layers.push(gj);
    } catch (e) {}
  };

  // A cross-origin Window throws on any property read; weed those out up front.
  const usable = (v) => {
    try {
      if (v === null || typeof v !== 'object') return false;
      void v.constructor;          // throws for cross-origin frames
      return true;
    } catch (e) { return false; }
  };

  const roots = [];
  let names = [];
  try { names = Object.getOwnPropertyNames(window); } catch (e) { names = []; }
  for (const k of names) {
    const v = safe(window, k);
    if (usable(v)) roots.push(v);
  }

  // --- Leaflet -----------------------------------------------------------
  if (safe(window, 'L')) {
    result.libs.push('leaflet');
    for (const v of roots) {
      if (!safe(v, '_layers') || !safe(v, '_container')) continue;
      if (typeof safe(v, 'eachLayer') !== 'function') continue;
      callSafe(v, 'eachLayer', (layer) => {
        if (!usable(layer)) return;
        const gj = callSafe(layer, 'toGeoJSON');
        if (gj) push(gj);
        const u = safe(layer, '_url');
        if (u) result.urls.push(String(u));
      });
    }
  }

  // --- Mapbox GL / MapLibre ---------------------------------------------
  for (const v of roots) {
    if (typeof safe(v, 'getStyle') !== 'function') continue;
    if (typeof safe(v, 'queryRenderedFeatures') !== 'function') continue;
    result.libs.push('mapbox-gl');
    const style = callSafe(v, 'getStyle');
    const sources = safe(style || {}, 'sources') || {};
    let entries = [];
    try { entries = Object.entries(sources); } catch (e) { entries = []; }
    for (const [, src] of entries) {
      if (!usable(src)) continue;
      const data = safe(src, 'data');
      if (typeof data === 'string') result.urls.push(data);
      else if (data) push(data);
      const u = safe(src, 'url');
      if (u) result.urls.push(String(u));
      const tiles = safe(src, 'tiles') || [];
      try { for (const t of tiles) result.urls.push(String(t)); } catch (e) {}
    }
  }

  // --- Google Maps Data layer -------------------------------------------
  if (safe(safe(window, 'google') || {}, 'maps')) {
    result.libs.push('google-maps');
    for (const v of roots) {
      const data = safe(v, 'data');
      if (!usable(data) || typeof safe(data, 'forEach') !== 'function') continue;
      const feats = [];
      callSafe(data, 'forEach', (f) => {
        const geom = callSafe(f, 'getGeometry');
        if (!geom) return;
        const props = {};
        callSafe(f, 'forEachProperty', (val, key) => { props[key] = val; });
        const coordsOf = (g) => {
          const t = callSafe(g, 'getType');
          if (t === 'Point') {
            const p = callSafe(g, 'get');
            return p ? [callSafe(p, 'lng'), callSafe(p, 'lat')] : null;
          }
          const arr = callSafe(g, 'getArray');
          if (!arr) return null;
          if (t === 'LineString' || t === 'LinearRing')
            return arr.map((p) => [callSafe(p, 'lng'), callSafe(p, 'lat')]);
          return arr.map(coordsOf);
        };
        const c = coordsOf(geom);
        const t = callSafe(geom, 'getType');
        if (c && t) feats.push({ type: 'Feature', properties: props,
                                 geometry: { type: t, coordinates: c } });
      });
      if (feats.length) push({ type: 'FeatureCollection', features: feats });
    }
  }

  // --- OpenLayers --------------------------------------------------------
  const ol = safe(window, 'ol');
  if (ol) {
    result.libs.push('openlayers');
    let fmt = null;
    try { fmt = new ol.format.GeoJSON(); } catch (e) { fmt = null; }
    for (const v of roots) {
      if (typeof safe(v, 'getLayers') !== 'function' || !fmt) continue;
      const layers = callSafe(v, 'getLayers');
      callSafe(layers, 'forEach', (layer) => {
        const src = callSafe(layer, 'getSource');
        if (!usable(src)) return;
        const fs = callSafe(src, 'getFeatures');
        if (fs && fs.length) {
          try {
            push(JSON.parse(fmt.writeFeatures(fs, {
              featureProjection: 'EPSG:3857', dataProjection: 'EPSG:4326' })));
          } catch (e) {}
        }
        const u = callSafe(src, 'getUrl');
        if (typeof u === 'string') result.urls.push(u);
      });
    }
  }

  const t = safe(window, '__tpmap') || {};
  try { result.urls = result.urls.concat(t.kmlUrls || [], t.dataUrls || []); } catch (e) {}
  try {
    result.libs = Array.from(new Set(result.libs));
    result.urls = Array.from(new Set(result.urls.filter(Boolean)));
  } catch (e) {}
  return result;
}
"""

# Server-rendered apps (Next.js, Nuxt, Remix...) ship their page data as inline
# JSON rather than fetching it, so no amount of network watching will see it.
INLINE_JSON_SCRIPT = r"""
() => {
  const out = [];
  const push = (source, text) => {
    try {
      if (typeof text === 'string' && text.length > 40 && text.length < 20000000)
        out.push({ source: source, text: text });
    } catch (e) {}
  };
  try {
    for (const s of document.querySelectorAll('script')) {
      const type = (s.getAttribute('type') || '').toLowerCase();
      const id = s.getAttribute('id') || '';
      if (type.indexOf('json') !== -1 || id === '__NEXT_DATA__')
        push(id || type || 'inline-script', s.textContent);
    }
  } catch (e) {}
  for (const key of ['__NEXT_DATA__', '__NUXT__', '__INITIAL_STATE__',
                     '__APOLLO_STATE__', '__remixContext', '__SERVER_DATA__',
                     '__staticRouterHydrationData', '__PRELOADED_STATE__']) {
    try {
      const v = window[key];
      if (v) push('window.' + key, JSON.stringify(v));
    } catch (e) {}
  }
  return out;
}
"""


# Controls that commonly reveal extra layers once toggled.
LAYER_TOGGLE_SELECTORS = [
    ".leaflet-control-layers-toggle",
    ".leaflet-control-layers input[type=checkbox]",
    "[class*='layer'] input[type=checkbox]",
    "[class*='legend'] input[type=checkbox]",
    "button[class*='layer']",
    "[aria-label*='layer' i]",
]


def chromium_executable() -> str | None:
    """An explicit Chromium binary, if one is configured or obviously present.

    Playwright normally manages its own browser download, but sandboxes and CI
    images often ship one already; TPMAP_CHROMIUM points at it.
    """
    for var in ("TPMAP_CHROMIUM", "PLAYWRIGHT_CHROMIUM_EXECUTABLE"):
        value = os.environ.get(var)
        if value and os.path.exists(value):
            return value
    browsers_dir = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if browsers_dir:
        candidate = os.path.join(browsers_dir, "chromium")
        if os.path.exists(candidate):
            return candidate
    return None



def cdp_endpoints(url: str) -> list[str]:
    """Candidate CDP URLs to try, in order.

    Chrome binds its debugging port to IPv4 127.0.0.1 only, while "localhost"
    resolves to IPv6 ::1 first on Windows -- so the obvious URL is refused.
    Try the loopback spellings rather than making the user work that out.
    """
    parsed = urlparse(url if "://" in url else f"http://{url}")
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9222

    hosts = [host]
    if host in ("localhost", "127.0.0.1", "::1"):
        hosts = ["127.0.0.1", "localhost", "[::1]"]
    return [f"{scheme}://{h}:{port}" for h in dict.fromkeys(hosts)]


class CdpUnreachable(Exception):
    """No Chrome is listening for a debugger on the given endpoint."""


def _open_context(pw, *, headed, executable_path, storage_state, cdp_url,
                  profile_dir, user_agent):
    """Return (context, close_fn).

    Three ways in, in order of fidelity:

    cdp_url      attach to a Chrome the user started themselves. Nothing is
                 automated at launch, so logins that refuse to run under
                 automation (reCAPTCHA, Firebase phone auth) work normally, and
                 IndexedDB -- where Firebase Auth actually keeps its token --
                 is live rather than needing to be serialised.
    profile_dir  drive a real Chrome profile on disk, so an existing signed-in
                 session is reused with no login at all. Chrome must be closed.
    otherwise    a clean throwaway browser, optionally seeded with a saved
                 storage_state.
    """
    ctx_args = {
        "ignore_https_errors": True,
        "user_agent": user_agent or BROWSER_UA,
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-IN",
    }

    if cdp_url:
        from playwright.sync_api import Error as PWError

        last = None
        for endpoint in cdp_endpoints(cdp_url):
            try:
                browser = pw.chromium.connect_over_cdp(endpoint)
            except PWError as exc:
                log.debug("cdp connect failed for %s: %s", endpoint, exc)
                last = exc
                continue
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            log.info("attached to your browser at %s", endpoint)
            # Never close a browser we did not start.
            return context, (lambda: None)

        tried = ", ".join(cdp_endpoints(cdp_url))
        raise CdpUnreachable(
            f"no Chrome is listening for a debugger (tried {tried}).\n"
            f"      1. Quit Chrome completely -- check Task Manager for chrome.exe. "
            f"The --remote-debugging-port flag is ignored if Chrome is already running.\n"
            f"      2. Start it again with: "
            f'chrome.exe --remote-debugging-port=9222\n'
            f"      3. Confirm it is up by opening http://127.0.0.1:9222/json/version "
            f"in that browser.\n"
            f"      Underlying error: {last}")

    if profile_dir:
        launch = {"headless": not headed}
        if executable_path:
            launch["executable_path"] = executable_path
        context = pw.chromium.launch_persistent_context(
            str(profile_dir), **launch, **ctx_args)
        log.info("using Chrome profile %s", profile_dir)
        return context, context.close

    launch = {"headless": not headed}
    exe = executable_path or chromium_executable()
    if exe:
        launch["executable_path"] = exe
    browser = pw.chromium.launch(**launch)
    if storage_state and os.path.exists(str(storage_state)):
        ctx_args["storage_state"] = str(storage_state)
        log.info("using saved session %s", storage_state)
    context = browser.new_context(**ctx_args)
    return context, browser.close


def discover_page(url, *, headed=False, wait=6.0, timeout=45000,
                  user_agent=None, click_layers=False, capture=True,
                  executable_path=None, storage_state=None,
                  cdp_url=None, profile_dir=None):
    """Load ``url`` in a browser and report every geodata endpoint it touches.

    Returns ``(Report, {url: body})`` -- bodies are the responses we already
    captured, so the caller can avoid re-fetching them.
    """
    from playwright.sync_api import Error as PWError, TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    report = Report(page_url=url)
    bodies: dict[str, bytes] = {}
    text_blobs: list[tuple[str, str]] = []   # (url, source text) for the regex scan
    captured_total = 0

    def on_response(response):
        nonlocal captured_total
        try:
            rurl = response.url
            if rurl.startswith("data:"):
                return
            headers = response.headers
            ctype = headers.get("content-type", "")
            rtype = getattr(response.request, "resource_type", "")
            if rtype in ("image", "media", "font", "stylesheet"):
                return

            body = None
            if capture and captured_total < MAX_CAPTURE:
                try:
                    body = response.body()
                    captured_total += len(body)
                except Exception:
                    body = None

            # keep HTML/JS text around so we can grep it for literal KML links
            if body and (rtype in ("document", "script") or "javascript" in ctype
                         or "html" in ctype):
                if len(body) < 8 * 1024 * 1024:
                    text_blobs.append((rurl, body.decode("utf-8", errors="replace")))

            if len(report.responses) < 400:
                report.responses.append({
                    "url": rurl[:400], "type": rtype, "content_type": ctype,
                    "status": getattr(response, "status", 0),
                    "size": len(body) if body else 0})

            kind = classify(rurl, ctype, body)
            if kind:
                report.add(Hit(url=rurl, kind=kind, source="network",
                               content_type=ctype, size=len(body) if body else 0))
                if body:
                    bodies[rurl] = body
        except Exception as exc:                      # never break the page load
            log.debug("response handler error: %s", exc)

    with sync_playwright() as pw:
        try:
            context, close = _open_context(
                pw, headed=headed, executable_path=executable_path,
                storage_state=storage_state, cdp_url=cdp_url,
                profile_dir=profile_dir, user_agent=user_agent)
        except CdpUnreachable as exc:
            report.errors.append(str(exc))
            return report, bodies
        except PWError as exc:
            report.errors.append(f"could not start a browser: {exc}")
            return report, bodies
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()
        page.on("response", on_response)

        main_response = None
        try:
            main_response = page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except PWTimeout:
            report.errors.append(f"navigation timeout after {timeout}ms")
        except PWError as exc:
            report.errors.append(f"navigation failed: {exc}")
            close()
            return report, bodies

        # Whatever else happens, say plainly if the page itself did not load.
        if main_response is not None:
            status = getattr(main_response, "status", 0)
            if status >= 400:
                report.errors.append(
                    f"the page returned HTTP {status} -- check the URL is right "
                    f"and reachable in a normal browser")

        try:
            page.wait_for_load_state("networkidle", timeout=int(wait * 1000))
        except PWTimeout:
            pass          # map apps often poll forever; not an error

        # Nudge lazy layers into loading.
        try:
            page.mouse.wheel(0, 2000)
            page.wait_for_timeout(500)
        except PWError:
            pass

        if click_layers:
            for sel in LAYER_TOGGLE_SELECTORS:
                try:
                    for el in page.query_selector_all(sel)[:12]:
                        try:
                            el.click(timeout=1500, force=True)
                            page.wait_for_timeout(350)
                        except PWError:
                            continue
                except PWError:
                    continue

        page.wait_for_timeout(int(wait * 1000))

        # -- page probe, across every frame ---------------------------------
        # The map is often inside an iframe, so the main frame alone is not enough.
        frames = [page]
        try:
            frames += [f for f in page.frames if f is not page.main_frame]
        except PWError:
            pass

        for frame in frames:
            try:
                probe = frame.evaluate(PROBE_SCRIPT)
            except PWError as exc:
                report.errors.append(f"page probe failed: {str(exc)[:200]}")
                continue
            if not probe:
                continue
            if probe.get("libs"):
                log.info("map libraries detected: %s", ", ".join(probe["libs"]))
            for layer in probe.get("layers", []):
                if isinstance(layer, dict):
                    report.inline.append(layer)
            for u in probe.get("urls", []):
                absolute = urljoin(url, u)
                kind = classify(absolute) or "geojson"
                report.add(Hit(url=absolute, kind=kind, source="init-hook",
                               note="referenced by in-page map object"))

        # -- inline JSON (Next.js and friends embed page data, never fetch it) --
        from .coerce import coerce_to_feature_collection
        from .kml import ConversionError

        for frame in frames:
            try:
                blocks = frame.evaluate(INLINE_JSON_SCRIPT) or []
            except PWError as exc:
                report.errors.append(f"inline json scan failed: {str(exc)[:200]}")
                continue
            for block in blocks:
                text = block.get("text") or ""
                try:
                    obj = json.loads(text)
                except ValueError:
                    continue
                try:
                    fc = coerce_to_feature_collection(obj)
                except ConversionError:
                    continue
                log.info("inline JSON %s -> %d feature(s)",
                         block.get("source"), len(fc["features"]))
                report.inline.append(fc)
                report.add(Hit(url=f"{url}#{block.get('source')}", kind="embedded",
                               source="inline-json", size=len(text),
                               note="geodata embedded in the page's own HTML"))

        # Landing somewhere else means the page is gated, and no amount of
        # waiting will produce a map.
        try:
            final = page.url
        except PWError:
            final = url
        if urlparse(final).path.rstrip("/") != urlparse(url).path.rstrip("/"):
            landing = urlparse(final).path.rstrip("/").lower()
            if landing in ("/home", "/dashboard", "/app", "/maps", ""):
                # Being sent to the app's own home page means the session is fine
                # and the URL simply is not a page.
                report.errors.append(
                    f"redirected to {final} -- you are signed in, so this URL is "
                    f"most likely not a real page. Open the scheme you want in the "
                    f"browser and run with --current instead of a URL")
            else:
                report.errors.append(
                    f"redirected to {final} -- this page is gated. Sign in with "
                    f"`tpmap browser`, then re-run with --cdp auto")

        # -- source scan ----------------------------------------------------
        try:
            html = page.content()
            text_blobs.append((url, html))
            if not report.hits and not report.inline:
                head = html[:4000]
                if CHALLENGE_RE.search(head):
                    report.errors.append(
                        "the site served a bot-protection interstitial, not the page "
                        "-- retry with --headed so you can clear it yourself")
                elif len(html) < 1200:
                    report.errors.append(
                        f"the page returned only {len(html)} bytes of HTML; it is "
                        f"probably rendered after login or by a route this URL misses")
        except PWError:
            pass
        for base, text in text_blobs:
            for found in scan_source(text, base):
                kind = classify(found) or "kml"
                report.add(Hit(url=found, kind=kind, source="source-scan",
                               note="literal reference in page source"))

        try:
            page.close()
        except PWError:
            pass
        close()

    return report, bodies


def save_login_state(url, session_path, *, executable_path=None, timeout=45000,
                     cdp_url=None, profile_dir=None):
    """Open a browser so the user can sign in, then persist the session.

    Nothing is automated: the person logs in themselves with their own
    credentials, and only the resulting cookies and storage are saved.
    """
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    warnings = []
    with sync_playwright() as pw:
        context, close = _open_context(
            pw, headed=True, executable_path=executable_path, storage_state=None,
            cdp_url=cdp_url, profile_dir=profile_dir, user_agent=None)
        pages = getattr(context, "pages", None) or []
        page = pages[0] if (cdp_url and pages) else context.new_page()
        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except PWError as exc:
            log.warning("could not open %s: %s", url, exc)

        print("\nA browser window is open.")
        print("  1. Sign in / dismiss whatever is blocking the page")
        print("  2. Navigate to a scheme map and let it finish loading")
        print("  3. Come back here and press Enter\n")
        try:
            input("Press Enter once you are signed in... ")
        except EOFError:
            pass

        try:
            final = page.url
        except PWError:
            final = url

        # Firebase Auth keeps its token in IndexedDB, which storage_state does
        # not capture -- saying so now beats a mystifying failure later.
        try:
            dbs = page.evaluate(
                "async () => (indexedDB.databases ? (await indexedDB.databases())"
                ".map(d => d.name || '') : [])")
        except PWError:
            dbs = []
        if any("firebase" in str(d).lower() for d in dbs) and not profile_dir:
            warnings.append(
                "This site keeps its login in IndexedDB (Firebase Auth), which a "
                "saved session file cannot carry. Use --profile or --cdp instead; "
                "see the README section on pages behind a sign-in.")

        try:
            context.storage_state(path=str(session_path))
        except PWError as exc:
            warnings.append(f"could not write the session file: {exc}")
        close()

    return final, warnings


def list_tabs(endpoint: str) -> list[dict]:
    """Every target Chrome reports, most-recently-used first."""
    import json as _json
    import urllib.request
    for ep in cdp_endpoints(endpoint):
        try:
            with urllib.request.urlopen(f"{ep}/json/list", timeout=5) as resp:
                return _json.load(resp)
        except Exception as exc:
            log.debug("could not list tabs at %s: %s", ep, exc)
    return []


def _is_real_page(tab: dict) -> bool:
    url = tab.get("url", "")
    return (tab.get("type") == "page"
            and not url.startswith(("about:", "chrome:", "devtools:", "edge:"))
            and url != "")


def current_tab(cdp_url: str) -> tuple[str, list[str]]:
    """The URL of the page open in the attached browser, plus its map links.

    Guessing URLs is unreliable; letting the user navigate to the scheme they
    actually want and reading it back is not.

    Chrome's own tab listing is the source of truth here -- it is ordered
    most-recently-used, and it sees tabs that Playwright's context enumeration
    can miss.
    """
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    tabs = list_tabs(cdp_url)
    real = [t for t in tabs if _is_real_page(t)]
    if not real:
        blank = [t for t in tabs if t.get("type") == "page"]
        if blank:
            raise CdpUnreachable(
                f"the browser is running but nothing is loaded in it "
                f"({len(blank)} blank tab(s)). Open the scheme you want in that "
                f"window, then run this again.")
        raise CdpUnreachable(
            "the browser is running but has no tabs open. Its window was probably "
            "closed. Reopen it with:  tpmap browser --open https://townplanmap.com")

    url = real[0]["url"]

    # Links are a bonus; failing to read them must not lose the URL.
    links: list[str] = []
    try:
        with sync_playwright() as pw:
            last = None
            for ep in cdp_endpoints(cdp_url):
                try:
                    browser = pw.chromium.connect_over_cdp(ep)
                    break
                except PWError as exc:
                    last = exc
            else:
                log.debug("could not attach for link extraction: %s", last)
                return url, links

            pages = []
            for ctx in browser.contexts:
                pages.extend(ctx.pages)
            page = next((p for p in pages if p.url == url), None)
            if page is not None:
                links = page.evaluate("""
                    () => Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.startsWith('http'))
                """) or []
    except Exception as exc:
        log.debug("link extraction failed: %s", exc)

    return url, list(dict.fromkeys(links))


def open_tab(cdp_url: str, url: str) -> str:
    """Open a page in the attached browser and return its URL."""
    from playwright.sync_api import Error as PWError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        last = None
        for ep in cdp_endpoints(cdp_url):
            try:
                browser = pw.chromium.connect_over_cdp(ep)
                break
            except PWError as exc:
                last = exc
        else:
            raise CdpUnreachable(f"could not attach to {cdp_url}: {last}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except PWError as exc:
            log.warning("could not load %s: %s", url, exc)
        return page.url
