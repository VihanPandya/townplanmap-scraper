"""ArcGIS harvesting, driven by a stub fetcher (no network)."""

import json
from urllib.parse import parse_qs, urlparse

import pytest

from tpmap.arcgis import harvest_services, list_layers, query_layer, service_root


class StubFetcher:
    """Serves canned FeatureServer responses and records what was asked for."""

    def __init__(self, total=2500, page=1000, reject_geojson=False):
        self.total, self.page, self.reject_geojson = total, page, reject_geojson
        self.calls = []

    def get_text(self, url):
        self.calls.append(url)
        q = parse_qs(urlparse(url).query)
        if q.get("f", [""])[0] == "json" and "/query" not in url:
            return json.dumps({"layers": [{"id": 0}, {"id": 3}], "tables": []})

        fmt = q.get("f", ["geojson"])[0]
        if fmt == "geojson" and self.reject_geojson:
            return json.dumps({"error": {"code": 400, "message": "unsupported format"}})

        offset = int(q.get("resultOffset", ["0"])[0])
        size = int(q.get("resultRecordCount", ["1000"])[0])
        n = max(0, min(size, self.total - offset))
        if fmt == "json":
            return json.dumps({
                "spatialReference": {"wkid": 4326},
                "exceededTransferLimit": offset + n < self.total,
                "features": [{"attributes": {"OBJECTID": offset + i},
                              "geometry": {"x": 72.5, "y": 23.0}} for i in range(n)],
            })
        return json.dumps({
            "type": "FeatureCollection",
            "exceededTransferLimit": offset + n < self.total,
            "features": [{"type": "Feature", "properties": {"OBJECTID": offset + i},
                          "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}
                         for i in range(n)],
        })


@pytest.mark.parametrize("url,expect", [
    ("https://h/arcgis/rest/services/TP/FeatureServer/2/query?where=1%3D1",
     ("https://h/arcgis/rest/services/TP/FeatureServer", 2)),
    ("https://h/arcgis/rest/services/TP/FeatureServer",
     ("https://h/arcgis/rest/services/TP/FeatureServer", None)),
    ("https://h/arcgis/rest/services/TP/MapServer/0",
     ("https://h/arcgis/rest/services/TP/MapServer", 0)),
])
def test_service_root_parsing(url, expect):
    assert service_root(url) == expect


def test_query_layer_pages_through_everything():
    f = StubFetcher(total=2500)
    fc = query_layer("https://h/x/FeatureServer", 0, f)
    assert len(fc["features"]) == 2500
    ids = [x["properties"]["OBJECTID"] for x in fc["features"]]
    assert ids == list(range(2500))          # no gaps, no repeats
    assert len(f.calls) == 3                 # 1000 + 1000 + 500


def test_query_layer_stops_on_a_short_final_page():
    f = StubFetcher(total=150)
    fc = query_layer("https://h/x/FeatureServer", 0, f)
    assert len(fc["features"]) == 150
    assert len(f.calls) == 1


def test_query_layer_falls_back_to_esri_json():
    f = StubFetcher(total=10, reject_geojson=True)
    fc = query_layer("https://h/x/FeatureServer", 0, f)
    assert len(fc["features"]) == 10
    assert fc["features"][0]["geometry"]["type"] == "Point"
    assert any("f=json" in c for c in f.calls)


def test_list_layers_includes_tables():
    assert list_layers("https://h/x/FeatureServer", StubFetcher()) == [0, 3]


def test_harvest_services_expands_and_dedupes_layers():
    f = StubFetcher(total=5)
    out = list(harvest_services([
        "https://h/x/FeatureServer",              # expands to layers 0 and 3
        "https://h/x/FeatureServer/0/query?f=geojson",   # already covered
    ], f))
    assert len(out) == 2
    assert all(len(fc["features"]) == 5 for fc in out)


def test_harvest_services_without_a_fetcher_is_a_noop():
    assert list(harvest_services(["https://h/x/FeatureServer"], None)) == []


def test_esri_fallback_is_sticky_across_pages():
    """Once a server rejects f=geojson, later pages should not ask again."""
    f = StubFetcher(total=2500, reject_geojson=True)
    fc = query_layer("https://h/x/FeatureServer", 0, f)
    assert len(fc["features"]) == 2500
    # 3 pages + a single rejected geojson probe on the first page
    assert len(f.calls) == 4
    assert sum("f=geojson" in c for c in f.calls) == 1
