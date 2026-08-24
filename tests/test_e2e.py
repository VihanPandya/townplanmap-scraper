"""End-to-end runs against the local fixture site."""

import xml.etree.ElementTree as ET

import pytest

from tpmap.crawl import discover_pages
from tpmap.discover import discover_page
from tpmap.extract import harvest_page
from tpmap.net import Fetcher

NS = {"k": "http://www.opengis.net/kml/2.2"}


@pytest.fixture
def fetcher():
    with Fetcher(rate=0, obey_robots=True) as f:
        yield f


def test_sitemap_listing_finds_only_map_pages(site, fetcher):
    urls = discover_pages(fetcher, site + "/")
    assert sorted(urls) == [f"{site}/tp/scheme-a.html", f"{site}/tp/scheme-b.html"]


def test_link_crawl_fallback_reaches_scheme_pages(site, fetcher):
    from tpmap.crawl import crawl_links
    urls = crawl_links(fetcher, site + "/", max_pages=20)
    assert f"{site}/tp/scheme-a.html" in urls
    assert f"{site}/tp/scheme-b.html" in urls
    # boilerplate pages are filtered out by SKIP_PATH_RE
    assert f"{site}/about.html" not in urls


def test_discovery_uses_all_three_channels(site, browser_ok):
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, bodies = discover_page(f"{site}/tp/scheme-a.html", wait=2.0)

    by_source = {h.source for h in report.hits}
    kinds = {h.kind for h in report.hits}

    # network: the XHR-loaded GeoJSON and the Esri-shaped payload
    assert "network" in by_source
    assert {"geojson", "esri"} <= kinds
    # source-scan: a .kml referenced in script source but never fetched in-page
    assert any(h.kind == "kml" and h.source == "source-scan" for h in report.hits)
    # page-probe: features lifted out of the live map object
    assert report.inline and sum(len(l["features"]) for l in report.inline) == 2


def test_harvest_writes_deduped_kml(site, tmp_path, browser_ok, fetcher):
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-a.html", tmp_path, fetcher=fetcher, wait=2.0)
    assert res.ok, res.errors

    converted = [p for p in res.files if p.endswith("-converted.kml")]
    assert converted, res.files
    root = ET.parse(converted[0]).getroot()
    names = [e.text for e in root.findall(".//k:Placemark/k:name", NS)]
    # FP-1/FP-2 arrive via both the network and the page probe; each appears once
    assert names == ["FP-1", "FP-2", "18m Road"]

    # the site's own KML is passed through verbatim rather than re-serialised
    native = [p for p in res.files if p.endswith("-src1.kml")]
    assert native and "Zoning boundary" in open(native[0]).read()


def test_harvest_finds_kml_behind_a_plain_anchor(site, tmp_path, browser_ok, fetcher):
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-b.html", tmp_path, fetcher=fetcher, wait=1.5)
    assert res.ok, res.errors
    assert res.placemarks == 1


def test_harvest_reports_pages_with_no_geodata(site, tmp_path, browser_ok, fetcher):
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/about.html", tmp_path, fetcher=fetcher, wait=1.0)
    assert not res.ok
    assert "no geodata found on this page" in res.errors


def test_kmz_output_format(site, tmp_path, browser_ok, fetcher):
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-b.html", tmp_path, fetcher=fetcher,
                       wait=1.5, fmt="kmz")
    assert res.ok and all(p.endswith(".kmz") for p in res.files)
    from tpmap.kml import kmz_to_kml
    assert b"<kml" in kmz_to_kml(open(res.files[0], "rb").read())
