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


@dataclass
class Hit:
    """One discovered geodata endpoint."""
    url: str
    kind: str                 # kml | kmz | geojson | esri | arcgis-service | ogc-service | tiles
    source: str               # network | init-hook | source-scan | page-probe
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

    def add(self, hit: Hit) -> None:
        if all(hit.key() != h.key() for h in self.hits):
            self.hits.append(hit)

    def by_kind(self, *kinds: str) -> list[Hit]:
        return [h for h in self.hits if h.kind in kinds]

    def downloadable(self) -> list[Hit]:
        """Hits we can turn into KML by a plain GET, best format first."""
        order = {"kml": 0, "kmz": 1, "geojson": 2, "esri": 3}
        return sorted((h for h in self.hits if h.kind in order),
                      key=lambda h: order[h.kind])

    def to_dict(self) -> dict:
        return {
            "page_url": self.page_url,
            "hits": [asdict(h) for h in self.hits],
            "inline_layers": len(self.inline),
            "inline_features": sum(len(l.get("features", [])) for l in self.inline),
            "errors": self.errors,
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
PROBE_SCRIPT = r"""
() => {
  const result = { layers: [], urls: [], libs: [] };
  const push = (gj) => {
    if (!gj) return;
    if (gj.type === 'FeatureCollection' && (!gj.features || !gj.features.length)) return;
    result.layers.push(gj);
  };

  const roots = [];
  for (const k of Object.getOwnPropertyNames(window)) {
    let v; try { v = window[k]; } catch (e) { continue; }
    if (v && typeof v === 'object') roots.push(v);
  }

  // --- Leaflet -----------------------------------------------------------
  if (window.L) {
    result.libs.push('leaflet');
    for (const v of roots) {
      if (!v || !v._layers || !v._container || typeof v.eachLayer !== 'function') continue;
      try {
        v.eachLayer((layer) => {
          try {
            if (typeof layer.toGeoJSON === 'function') push(layer.toGeoJSON());
            if (layer._url) result.urls.push(String(layer._url));
          } catch (e) {}
        });
      } catch (e) {}
    }
  }

  // --- Mapbox GL / MapLibre ---------------------------------------------
  for (const v of roots) {
    if (!v || typeof v.getStyle !== 'function' || typeof v.queryRenderedFeatures !== 'function') continue;
    result.libs.push('mapbox-gl');
    let style; try { style = v.getStyle(); } catch (e) { continue; }
    for (const [id, src] of Object.entries((style && style.sources) || {})) {
      if (!src) continue;
      if (typeof src.data === 'string') result.urls.push(src.data);
      else if (src.data) push(src.data);
      if (src.url) result.urls.push(src.url);
      for (const t of (src.tiles || [])) result.urls.push(t);
    }
  }

  // --- Google Maps Data layer -------------------------------------------
  if (window.google && window.google.maps) {
    result.libs.push('google-maps');
    for (const v of roots) {
      if (!v || !v.data || typeof v.data.forEach !== 'function') continue;
      const feats = [];
      try {
        v.data.forEach((f) => {
          try {
            const geom = f.getGeometry(); if (!geom) return;
            const props = {};
            f.forEachProperty((val, key) => { props[key] = val; });
            const coordsOf = (g) => {
              const t = g.getType();
              if (t === 'Point') { const p = g.get(); return [p.lng(), p.lat()]; }
              if (t === 'LineString' || t === 'LinearRing')
                return g.getArray().map((p) => [p.lng(), p.lat()]);
              if (t === 'Polygon' || t === 'MultiLineString' || t === 'MultiPoint')
                return g.getArray().map(coordsOf);
              if (t === 'MultiPolygon') return g.getArray().map(coordsOf);
              return null;
            };
            const c = coordsOf(geom);
            if (c) feats.push({ type: 'Feature', properties: props,
                                geometry: { type: geom.getType(), coordinates: c } });
          } catch (e) {}
        });
      } catch (e) {}
      if (feats.length) push({ type: 'FeatureCollection', features: feats });
    }
  }

  // --- OpenLayers --------------------------------------------------------
  if (window.ol) {
    result.libs.push('openlayers');
    try {
      const fmt = new window.ol.format.GeoJSON();
      for (const v of roots) {
        if (!v || typeof v.getLayers !== 'function') continue;
        v.getLayers().forEach((layer) => {
          try {
            const src = layer.getSource && layer.getSource();
            if (src && typeof src.getFeatures === 'function') {
              const fs = src.getFeatures();
              if (fs && fs.length) push(JSON.parse(fmt.writeFeatures(fs, {
                featureProjection: 'EPSG:3857', dataProjection: 'EPSG:4326' })));
            }
            if (src && typeof src.getUrl === 'function') {
              const u = src.getUrl(); if (typeof u === 'string') result.urls.push(u);
            }
          } catch (e) {}
        });
      }
    } catch (e) {}
  }

  const t = window.__tpmap || {};
  result.urls = result.urls.concat(t.kmlUrls || [], t.dataUrls || []);
  result.libs = Array.from(new Set(result.libs));
  result.urls = Array.from(new Set(result.urls.filter(Boolean)));
  return result;
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


def discover_page(url, *, headed=False, wait=6.0, timeout=45000,
                  user_agent=None, click_layers=False, capture=True,
                  executable_path=None):
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

            kind = classify(rurl, ctype, body)
            if kind:
                report.add(Hit(url=rurl, kind=kind, source="network",
                               content_type=ctype, size=len(body) if body else 0))
                if body:
                    bodies[rurl] = body
        except Exception as exc:                      # never break the page load
            log.debug("response handler error: %s", exc)

    with sync_playwright() as pw:
        launch_args = {"headless": not headed}
        exe = executable_path or chromium_executable()
        if exe:
            launch_args["executable_path"] = exe
        browser = pw.chromium.launch(**launch_args)
        ctx_args = {"ignore_https_errors": True}
        if user_agent:
            ctx_args["user_agent"] = user_agent
        context = browser.new_context(**ctx_args)
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()
        page.on("response", on_response)

        try:
            page.goto(url, timeout=timeout, wait_until="domcontentloaded")
        except PWTimeout:
            report.errors.append(f"navigation timeout after {timeout}ms")
        except PWError as exc:
            report.errors.append(f"navigation failed: {exc}")
            browser.close()
            return report, bodies

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

        # -- page probe -----------------------------------------------------
        try:
            probe = page.evaluate(PROBE_SCRIPT)
        except PWError as exc:
            probe = None
            report.errors.append(f"page probe failed: {exc}")

        if probe:
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

        # -- source scan ----------------------------------------------------
        try:
            text_blobs.append((url, page.content()))
        except PWError:
            pass
        for base, text in text_blobs:
            for found in scan_source(text, base):
                kind = classify(found) or "kml"
                report.add(Hit(url=found, kind=kind, source="source-scan",
                               note="literal reference in page source"))

        browser.close()

    return report, bodies
