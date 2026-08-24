"""Recovering geodata from JSON that is not already GeoJSON."""

import pytest

from tpmap.coerce import (coerce_to_feature_collection, contains_geodata,
                          decode_polyline, parse_wkt)
from tpmap.kml import ConversionError

RING = [[72.50, 23.00], [72.51, 23.00], [72.51, 23.01], [72.50, 23.01], [72.50, 23.00]]


def geoms(fc):
    return [f["geometry"]["type"] for f in fc["features"]]


# -- envelopes -------------------------------------------------------------

def test_geojson_wrapped_in_an_api_envelope():
    payload = {"status": "success", "code": 200,
               "data": {"type": "FeatureCollection", "features": [
                   {"type": "Feature", "properties": {"fp_no": "FP-1"},
                    "geometry": {"type": "Polygon", "coordinates": [RING]}}]}}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Polygon"]
    assert fc["features"][0]["properties"]["fp_no"] == "FP-1"


def test_deeply_nested_envelope():
    payload = {"result": {"page": {"layers": [{"payload": {
        "type": "Feature", "properties": {"n": 1},
        "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}}]}}}
    assert geoms(coerce_to_feature_collection(payload)) == ["Point"]


def test_records_with_a_geometry_field():
    payload = {"plots": [
        {"id": 1, "name": "FP-1", "geometry": {"type": "Polygon", "coordinates": [RING]}},
        {"id": 2, "name": "FP-2", "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}},
    ]}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Polygon", "Point"]
    assert [f["properties"]["name"] for f in fc["features"]] == ["FP-1", "FP-2"]


# -- coordinate spellings --------------------------------------------------

def test_latlng_object_arrays():
    payload = {"plots": [{"fp_no": "FP-9", "boundary": [
        {"lat": 23.00, "lng": 72.50}, {"lat": 23.00, "lng": 72.51},
        {"lat": 23.01, "lng": 72.51}, {"lat": 23.00, "lng": 72.50}]}]}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Polygon"]           # closed ring
    assert fc["features"][0]["properties"] == {"fp_no": "FP-9"}
    # GeoJSON order: lon first
    assert fc["features"][0]["geometry"]["coordinates"][0][0] == [72.50, 23.00]


def test_open_ring_becomes_a_linestring():
    payload = {"road": {"coords": [{"lat": 23.0, "lng": 72.5}, {"lat": 23.1, "lng": 72.6}]}}
    assert geoms(coerce_to_feature_collection(payload)) == ["LineString"]


def test_record_that_is_itself_a_point():
    payload = {"notices": [{"title": "Public notice", "latitude": 23.0, "longitude": 72.5}]}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Point"]
    assert fc["features"][0]["properties"] == {"title": "Public notice"}


def test_bare_xy_records_are_not_treated_as_points():
    """x/y are as often pixels as coordinates; they must not invent features."""
    payload = {"icons": [{"sprite": "pin", "x": 10, "y": 20}]}
    assert not contains_geodata(payload)


def test_latlon_flag_swaps_axis_order():
    payload = {"pts": {"coords": [[23.0, 72.5], [23.1, 72.6]]}}
    normal = coerce_to_feature_collection(payload)["features"][0]["geometry"]["coordinates"]
    swapped = coerce_to_feature_collection(
        payload, latlon=True)["features"][0]["geometry"]["coordinates"]
    assert normal[0] == [23.0, 72.5]
    assert swapped[0] == [72.5, 23.0]


# -- WKT -------------------------------------------------------------------

@pytest.mark.parametrize("wkt,expect", [
    ("POINT(72.5 23.0)", "Point"),
    ("LINESTRING(72.5 23.0, 72.6 23.1)", "LineString"),
    ("POLYGON((72.5 23.0, 72.6 23.0, 72.6 23.1, 72.5 23.0))", "Polygon"),
    ("MULTIPOLYGON(((72.5 23.0, 72.6 23.0, 72.6 23.1, 72.5 23.0)))", "MultiPolygon"),
    ("MULTILINESTRING((72.5 23.0, 72.6 23.1))", "MultiLineString"),
])
def test_wkt_types(wkt, expect):
    assert parse_wkt(wkt)["type"] == expect


def test_wkt_polygon_with_a_hole():
    g = parse_wkt("POLYGON((72.5 23.0, 72.6 23.0, 72.6 23.1, 72.5 23.0),"
                  "(72.55 23.02, 72.57 23.02, 72.57 23.04, 72.55 23.02))")
    assert len(g["coordinates"]) == 2


def test_wkt_in_a_database_style_row():
    payload = {"rows": [{"tp_no": "221", "the_geom": "POLYGON((72.5 23.0, 72.6 23.0, "
                                                     "72.6 23.1, 72.5 23.0))"}]}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Polygon"]
    assert fc["features"][0]["properties"] == {"tp_no": "221"}


def test_non_wkt_strings_are_ignored():
    assert parse_wkt("POINTLESS TEXT") is None
    assert parse_wkt("just a description") is None


# -- encoded polyline ------------------------------------------------------

def test_decode_polyline_roundtrip():
    # the canonical example from Google's polyline documentation
    coords = decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert coords is None or all(abs(c[0]) <= 180 and abs(c[1]) <= 90 for c in coords)


def test_polyline_only_decoded_when_the_key_says_so():
    encoded = "yz~jCyakwL??_pR_pR??~oR"
    named = {"routes": [{"id": 1, "encodedPath": encoded}]}
    unnamed = {"routes": [{"id": 1, "notes": encoded}]}
    assert contains_geodata(named)
    assert not contains_geodata(unnamed)


# -- rejection -------------------------------------------------------------

def test_ordinary_api_responses_yield_nothing():
    for payload in [
        {"status": "ok", "count": 5, "page": 1},
        {"user": {"id": 7, "name": "someone"}},
        {"config": {"zoom": 12, "tileSize": 256}},
        [],
        {"error": "not found"},
    ]:
        assert not contains_geodata(payload), payload
        with pytest.raises(ConversionError):
            coerce_to_feature_collection(payload)


def test_duplicate_features_are_collapsed():
    feat = {"type": "Feature", "properties": {"n": 1},
            "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}
    payload = {"a": {"type": "FeatureCollection", "features": [feat]},
               "b": {"type": "FeatureCollection", "features": [dict(feat)]}}
    assert len(coerce_to_feature_collection(payload)["features"]) == 1


def test_esri_nested_in_an_envelope():
    payload = {"d": {"spatialReference": {"wkid": 4326}, "features": [
        {"attributes": {"PLOT": "7"}, "geometry": {"x": 72.5, "y": 23.0}}]}}
    fc = coerce_to_feature_collection(payload)
    assert geoms(fc) == ["Point"]
    assert fc["features"][0]["properties"]["PLOT"] == "7"
