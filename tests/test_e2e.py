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
    assert sorted(urls) == [f"{site}/tp/scheme-a.html", f"{site}/tp/scheme-b.html",
                            f"{site}/tp/scheme-c.html", f"{site}/tp/scheme-d.html"]


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
    # the message should tell the user what to try next, not just that it failed
    assert any("no geodata found" in e and "--headed" in e for e in res.errors)


def test_kmz_output_format(site, tmp_path, browser_ok, fetcher):
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-b.html", tmp_path, fetcher=fetcher,
                       wait=1.5, fmt="kmz")
    assert res.ok and all(p.endswith(".kmz") for p in res.files)
    from tpmap.kml import kmz_to_kml
    assert b"<kml" in kmz_to_kml(open(res.files[0], "rb").read())


def test_envelope_wrapped_api_yields_kml(site, tmp_path, browser_ok, fetcher):
    """The regression this whole coercion layer exists for.

    scheme-c serves no top-level GeoJSON at all -- geometry is buried in an API
    envelope, written as lat/lng objects and one WKT string. Before coercion
    this page produced a report and no KML.
    """
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-c.html", tmp_path, fetcher=fetcher, wait=2.5)
    assert res.ok, res.errors
    assert res.placemarks == 3

    root = ET.parse(res.files[0]).getroot()
    assert [e.text for e in root.findall(".//k:Placemark/k:name", NS)] == \
        ["FP-101", "FP-102", "FP-103"]
    # lon,lat order, not lat,lon
    first = root.find(".//k:coordinates", NS).text.split()[0]
    assert first.startswith("72.5,23")
    # attributes survive the conversion
    zones = {d.find("k:value", NS).text
             for d in root.findall(".//k:Data[@name='zone']", NS)}
    assert zones == {"Residential", "Commercial", "Public"}


def test_envelope_endpoint_is_classified_as_embedded(site, browser_ok):
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.5)
    assert [h.kind for h in report.hits] == ["embedded"]


def test_cross_origin_iframe_does_not_break_the_probe(site, browser_ok):
    """A cross-origin frame threw SecurityError and killed the whole probe.

    Reading any property of an opaque-origin Window raises, and one uncaught
    throw aborted the probe for the entire page.
    """
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/scheme-d.html", wait=2.5)
    assert not [e for e in report.errors if "page probe failed" in e]
    assert not [e for e in report.errors if "SecurityError" in e]


def test_inline_next_data_is_extracted(site, tmp_path, browser_ok, fetcher):
    """Server-rendered page data is never fetched, so no network hit exists."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    res = harvest_page(f"{site}/tp/scheme-d.html", tmp_path, fetcher=fetcher, wait=2.5)
    assert res.ok, res.errors
    assert res.placemarks == 2
    root = ET.parse(res.files[0]).getroot()
    assert [e.text for e in root.findall(".//k:Placemark/k:name", NS)] == \
        ["FP-201", "FP-202"]


def test_inline_json_hits_are_not_fetched(site, browser_ok):
    """Their pseudo-URL points at the HTML page; downloading it is nonsense."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/scheme-d.html", wait=2.5)
    inline_hits = [h for h in report.hits if h.source == "inline-json"]
    assert inline_hits and "__NEXT_DATA__" in inline_hits[0].url
    assert all(h.source != "inline-json" for h in report.downloadable())


def test_report_records_responses_for_diagnosis(site, browser_ok):
    """A run that finds nothing must still be diagnosable from the report."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/about.html", wait=1.5)
    assert not report.hits
    assert report.to_dict()["responses_seen"] >= 1
    assert any("about.html" in r["url"] for r in report.to_dict()["responses"])


def test_thin_page_is_explained_not_shrugged_at(site, browser_ok):
    """A blank result must say why, not just report zero."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/about.html", wait=1.5)
    assert not report.hits
    assert any("bytes of HTML" in e for e in report.errors)


def test_responses_carry_status_codes(site, browser_ok):
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.0)
    assert all("status" in r for r in report.responses)
    assert any(r["status"] == 200 for r in report.responses)


def test_missing_page_reports_its_status(site, browser_ok):
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/does-not-exist.html", wait=1.0)
    assert any("HTTP 404" in e for e in report.errors)


def test_redirect_to_a_landing_page_is_reported(site, browser_ok):
    """The live symptom: the scheme URL bounces to /welcome and no map loads."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/gated.html", wait=2.0)
    assert not report.hits
    gated = [e for e in report.errors if "redirected to" in e]
    assert gated and "welcome.html" in gated[0]
    # and it must say what to do about it
    assert "tpmap login" in gated[0]


def test_saved_session_is_loaded(site, tmp_path, browser_ok):
    """A session file from `tpmap login` must be accepted and applied."""
    if not browser_ok:
        pytest.skip("no usable chromium")
    import json as _json
    from urllib.parse import urlparse
    host = urlparse(site).hostname
    state = tmp_path / "session.json"
    state.write_text(_json.dumps({
        "cookies": [{"name": "tpmap_test", "value": "1", "domain": host,
                     "path": "/", "expires": -1, "httpOnly": False,
                     "secure": False, "sameSite": "Lax"}],
        "origins": []}))
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.0,
                              storage_state=str(state))
    assert [h.kind for h in report.hits] == ["embedded"]   # still works with a session


def test_a_missing_session_file_is_not_fatal(site, tmp_path, browser_ok):
    if not browser_ok:
        pytest.skip("no usable chromium")
    report, _ = discover_page(f"{site}/tp/scheme-c.html", wait=2.0,
                              storage_state=str(tmp_path / "nope.json"))
    assert [h.kind for h in report.hits] == ["embedded"]
