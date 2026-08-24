"""Bulk-export ArcGIS REST layers.

Municipal planning portals are very often ArcGIS-backed.  When discovery turns
up a FeatureServer/MapServer, the map itself only ever requests the features in
the current viewport -- this module pages through the whole layer instead.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlencode, urlparse, urlunparse

from .kml import ConversionError, to_feature_collection

log = logging.getLogger("tpmap.arcgis")

LAYER_RE = re.compile(r"^(.*/(?:Feature|Map)Server)(?:/(\d+))?", re.I)
PAGE_SIZE = 1000
MAX_PAGES = 200


def service_root(url: str) -> tuple[str, int | None]:
    """Split a service URL into (server root, layer id or None)."""
    clean = urlunparse(urlparse(url)._replace(query="", fragment=""))
    clean = re.sub(r"/query/?$", "", clean, flags=re.I)
    m = LAYER_RE.match(clean)
    if not m:
        return clean.rstrip("/"), None
    return m.group(1), (int(m.group(2)) if m.group(2) is not None else None)


def list_layers(root: str, fetcher) -> list[int]:
    """Layer ids exposed by a service."""
    try:
        meta = json.loads(fetcher.get_text(f"{root}?f=json"))
    except Exception as exc:
        log.debug("service metadata failed for %s: %s", root, exc)
        return []
    ids = [lyr["id"] for lyr in meta.get("layers", []) if "id" in lyr]
    ids += [t["id"] for t in meta.get("tables", []) if "id" in t]
    return ids


def query_layer(root: str, layer_id: int, fetcher, *, page_size: int = PAGE_SIZE) -> dict:
    """Page through one layer and return a single FeatureCollection."""
    features: list[dict] = []
    offset = 0
    fmt = "geojson"          # sticky: once a server rejects geojson, stop asking

    for _ in range(MAX_PAGES):
        params = {
            "where": "1=1",
            "outFields": "*",
            "outSR": "4326",
            "returnGeometry": "true",
            "f": fmt,
            "resultOffset": offset,
            "resultRecordCount": page_size,
        }
        url = f"{root}/{layer_id}/query?{urlencode(params)}"
        try:
            payload = json.loads(fetcher.get_text(url))
        except Exception as exc:
            log.warning("layer %s/%s query failed: %s", root, layer_id, exc)
            break

        # Older servers reject f=geojson; fall back to Esri JSON for good.
        if isinstance(payload, dict) and payload.get("error"):
            fmt = params["f"] = "json"
            url = f"{root}/{layer_id}/query?{urlencode(params)}"
            try:
                payload = json.loads(fetcher.get_text(url))
            except Exception as exc:
                log.warning("layer %s/%s esri retry failed: %s", root, layer_id, exc)
                break
            if payload.get("error"):
                log.warning("layer %s/%s: %s", root, layer_id,
                            payload["error"].get("message", "error"))
                break

        try:
            fc = to_feature_collection(payload)
        except ConversionError as exc:
            log.warning("layer %s/%s unusable: %s", root, layer_id, exc)
            break

        batch = fc.get("features", [])
        features.extend(batch)
        log.info("arcgis %s/%s: +%d features (total %d)", root, layer_id,
                 len(batch), len(features))

        exceeded = payload.get("exceededTransferLimit") or payload.get("properties", {}).get(
            "exceededTransferLimit")
        if not batch or (not exceeded and len(batch) < page_size):
            break
        offset += len(batch)

    return {"type": "FeatureCollection", "features": features}


def harvest_services(urls, fetcher):
    """Yield a FeatureCollection per layer across every discovered service."""
    if fetcher is None:
        log.warning("arcgis harvesting needs an HTTP fetcher; skipping")
        return

    done: set[tuple[str, int]] = set()
    for url in urls:
        root, layer_id = service_root(url)
        layer_ids = [layer_id] if layer_id is not None else list_layers(root, fetcher)
        for lid in layer_ids:
            if (root, lid) in done:
                continue
            done.add((root, lid))
            fc = query_layer(root, lid, fetcher)
            if fc["features"]:
                yield fc
