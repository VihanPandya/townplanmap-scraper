"""Turning a page into KML on disk."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from . import kml as K
from .discover import discover_page
from .net import url_to_stem

log = logging.getLogger("tpmap.extract")


@dataclass
class PageResult:
    page_url: str
    files: list[str] = field(default_factory=list)
    features: int = 0
    placemarks: int = 0
    hits: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.files)


def _body_for(hit, bodies, fetcher):
    """Reuse what the browser already downloaded; only re-fetch if we must."""
    if hit.url in bodies:
        return bodies[hit.url]
    if fetcher is None:
        return None
    return fetcher.get_bytes(hit.url)


def harvest_page(page_url, outdir, *, fetcher=None, headed=False, wait=6.0,
                 click_layers=False, split=False, fmt="kml",
                 arcgis=False, save_report=True, user_agent=None,
                 executable_path=None) -> PageResult:
    """Discover, download and convert everything geodata-shaped on one page."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    result = PageResult(page_url=page_url)

    report, bodies = discover_page(
        page_url, headed=headed, wait=wait, click_layers=click_layers,
        user_agent=user_agent, executable_path=executable_path)
    result.hits = len(report.hits)
    result.errors.extend(report.errors)

    stem = url_to_stem(page_url)
    if save_report:
        rdir = outdir / "_reports"
        rdir.mkdir(exist_ok=True)
        (rdir / f"{stem}.json").write_text(
            json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    raw_docs: list[tuple[str, bytes]] = []      # native KML, kept verbatim
    collections: list[dict] = []                # everything else, normalised

    for hit in report.downloadable():
        try:
            blob = _body_for(hit, bodies, fetcher)
            if not blob:
                continue

            if hit.kind == "kmz":
                blob = K.kmz_to_kml(blob)
                raw_docs.append((hit.url, blob))
            elif hit.kind == "kml":
                raw_docs.append((hit.url, blob))
            else:
                obj = json.loads(blob.decode("utf-8", errors="replace"))
                collections.append(K.to_feature_collection(obj))
        except Exception as exc:
            msg = f"{hit.kind} {hit.url}: {exc}"
            log.warning("could not use %s", msg)
            result.errors.append(msg)

    # GeoJSON lifted straight out of the live map objects.
    for layer in report.inline:
        try:
            collections.append(K.to_feature_collection(layer))
        except K.ConversionError:
            continue

    if arcgis:
        from .arcgis import harvest_services
        services = [h.url for h in report.by_kind("arcgis-service")]
        for fc in harvest_services(services, fetcher=fetcher):
            collections.append(fc)

    # -- write ------------------------------------------------------------
    def write(name: str, blob: bytes) -> None:
        if fmt == "kmz":
            path = outdir / f"{name}.kmz"
            path.write_bytes(K.kml_to_kmz(blob.decode("utf-8", "replace")))
        else:
            path = outdir / f"{name}.kml"
            path.write_bytes(blob)
        result.files.append(str(path))
        result.placemarks += K.count_placemarks(blob)

    for i, (src, blob) in enumerate(raw_docs):
        # Native KML is the site's own authored document -- pass it through
        # untouched rather than round-tripping and losing its styling.
        name = f"{stem}-src{i + 1}" if len(raw_docs) > 1 or collections else stem
        write(name, blob)
        log.info("saved native KML from %s", src)

    if collections:
        total = sum(len(c.get("features", [])) for c in collections)
        result.features += total
        if split and len(collections) > 1:
            for i, fc in enumerate(K.dedupe_collections(collections), start=1):
                write(f"{stem}-layer{i}",
                      K.feature_collection_to_kml(fc, f"{stem} layer {i}").encode("utf-8"))
        else:
            merged = K.merge_feature_collections(collections)
            if merged.get("features"):
                suffix = "-converted" if raw_docs else ""
                write(f"{stem}{suffix}",
                      K.feature_collection_to_kml(merged, page_url).encode("utf-8"))

    if not result.files:
        result.errors.append("no geodata found on this page")
    return result
