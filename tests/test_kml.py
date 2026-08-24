import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, ".")
from tpmap import kml as K

NS = {"k": "http://www.opengis.net/kml/2.2"}


def parse(text):
    return ET.fromstring(text)


def test_mercator_roundtrip():
    lon, lat = K.mercator_to_wgs84(8092000.0, 2645000.0)
    assert 72.0 < lon < 73.5, lon          # Ahmedabad-ish longitude
    assert 22.5 < lat < 23.5, lat          # Ahmedabad-ish latitude


def test_polygon_with_hole():
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {"fp_no": "FP-12", "area": 1500},
        "geometry": {"type": "Polygon", "coordinates": [
            [[72.0, 23.0], [72.1, 23.0], [72.1, 23.1], [72.0, 23.1], [72.0, 23.0]],
            [[72.04, 23.04], [72.06, 23.04], [72.06, 23.06], [72.04, 23.06], [72.04, 23.04]],
        ]},
    }]}
    root = parse(K.feature_collection_to_kml(fc))
    assert len(root.findall(".//k:outerBoundaryIs", NS)) == 1
    assert len(root.findall(".//k:innerBoundaryIs", NS)) == 1
    # name promoted from the fp_no attribute
    assert root.find(".//k:Placemark/k:name", NS).text == "FP-12"
    # attributes preserved as ExtendedData
    names = {d.get("name") for d in root.findall(".//k:Data", NS)}
    assert names == {"fp_no", "area"}


def test_all_geometry_types_render():
    geoms = [
        {"type": "Point", "coordinates": [72.5, 23.0]},
        {"type": "MultiPoint", "coordinates": [[72.5, 23.0], [72.6, 23.1]]},
        {"type": "LineString", "coordinates": [[72.5, 23.0], [72.6, 23.1]]},
        {"type": "MultiLineString", "coordinates": [[[72.5, 23.0], [72.6, 23.1]]]},
        {"type": "MultiPolygon", "coordinates": [[[[72.0, 23.0], [72.1, 23.0], [72.1, 23.1], [72.0, 23.0]]]]},
        {"type": "GeometryCollection", "geometries": [{"type": "Point", "coordinates": [72.5, 23.0]}]},
    ]
    fc = {"type": "FeatureCollection",
          "features": [{"type": "Feature", "properties": {}, "geometry": g} for g in geoms]}
    root = parse(K.feature_collection_to_kml(fc))
    assert len(root.findall(".//k:Placemark", NS)) == len(geoms)


def test_altitude_is_preserved():
    fc = K.to_feature_collection({"type": "Point", "coordinates": [72.5, 23.0, 55.0]})
    root = parse(K.feature_collection_to_kml(fc))
    assert root.find(".//k:Point/k:coordinates", NS).text == "72.5,23,55"


def test_esri_polygon_rings_split_outer_and_hole():
    payload = {
        "spatialReference": {"wkid": 4326},
        "features": [{
            "attributes": {"PLOT_NO": "7"},
            # clockwise outer, counter-clockwise hole
            "geometry": {"rings": [
                [[72.0, 23.0], [72.0, 23.1], [72.1, 23.1], [72.1, 23.0], [72.0, 23.0]],
                [[72.04, 23.04], [72.06, 23.04], [72.06, 23.06], [72.04, 23.06], [72.04, 23.04]],
            ]},
        }],
    }
    fc = K.esri_to_geojson(payload)
    assert fc["features"][0]["geometry"]["type"] == "Polygon"
    assert len(fc["features"][0]["geometry"]["coordinates"]) == 2
    root = parse(K.feature_collection_to_kml(fc))
    assert len(root.findall(".//k:innerBoundaryIs", NS)) == 1
    assert root.find(".//k:Placemark/k:name", NS).text == "7"


def test_esri_webmercator_is_unprojected():
    payload = {
        "spatialReference": {"wkid": 102100},
        "features": [{"attributes": {}, "geometry": {"x": 8092000.0, "y": 2645000.0}}],
    }
    fc = K.esri_to_geojson(payload)
    lon, lat = fc["features"][0]["geometry"]["coordinates"]
    assert 72.0 < lon < 73.5 and 22.5 < lat < 23.5


def test_esri_paths_and_points():
    fc = K.esri_to_geojson({"features": [
        {"attributes": {}, "geometry": {"paths": [[[72.0, 23.0], [72.1, 23.1]]]}},
        {"attributes": {}, "geometry": {"paths": [[[72.0, 23.0]], [[72.5, 23.5]]]}},
        {"attributes": {}, "geometry": {"points": [[72.0, 23.0]]}},
    ]})
    types = [f["geometry"]["type"] for f in fc["features"]]
    assert types == ["LineString", "MultiLineString", "MultiPoint"]


def test_sniffers():
    assert K.looks_like_geojson({"type": "FeatureCollection", "features": []})
    assert K.looks_like_geojson({"type": "Polygon", "coordinates": []})
    assert not K.looks_like_geojson({"type": "Something"})
    assert K.looks_like_esri({"features": [{"attributes": {}, "geometry": {"rings": []}}]})
    # a GeoJSON FeatureCollection must not be mistaken for Esri
    assert not K.looks_like_esri({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {}}]})


def test_kmz_roundtrip():
    text = K.feature_collection_to_kml(
        K.to_feature_collection({"type": "Point", "coordinates": [72.5, 23.0]}))
    blob = K.kml_to_kmz(text)
    assert K.is_kmz_bytes(blob)
    assert K.kmz_to_kml(blob).decode() == text
    assert K.count_placemarks(text.encode()) == 1


def test_xml_escaping_of_hostile_attributes():
    fc = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "properties": {'we"ird': "<b>bold</b> & stuff"},
        "geometry": {"type": "Point", "coordinates": [72.5, 23.0]},
    }]}
    root = parse(K.feature_collection_to_kml(fc))   # must not raise
    assert root.find(".//k:Data", NS).get("name") == 'we"ird'
    assert root.find(".//k:Data/k:value", NS).text == "<b>bold</b> & stuff"


def test_merge_dedupes_overlapping_channels():
    feat = {"type": "Feature", "properties": {"fp_no": "FP-1"},
            "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}
    other = {"type": "Feature", "properties": {"fp_no": "FP-2"},
             "geometry": {"type": "Point", "coordinates": [72.6, 23.1]}}
    # same layer seen twice (network + page probe) plus one unique feature
    merged = K.merge_feature_collections([
        {"type": "FeatureCollection", "features": [feat, other]},
        {"type": "FeatureCollection", "features": [dict(feat)]},
    ])
    assert len(merged["features"]) == 2
    assert len(K.merge_feature_collections([
        {"type": "FeatureCollection", "features": [feat]},
        {"type": "FeatureCollection", "features": [dict(feat)]},
    ], dedupe=False)["features"]) == 2


def test_fingerprint_distinguishes_properties():
    a = {"geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {"n": 1}}
    b = {"geometry": {"type": "Point", "coordinates": [1, 2]}, "properties": {"n": 2}}
    assert K.feature_fingerprint(a) != K.feature_fingerprint(b)
    assert K.feature_fingerprint(a) == K.feature_fingerprint(dict(a))


def test_fingerprint_ignores_int_float_spelling():
    """The browser hands back 23 where JSON parsing gives 23.0."""
    from_json = {"geometry": {"type": "Point", "coordinates": [72.5, 23.0]},
                 "properties": {"n": 1}}
    from_browser = {"geometry": {"type": "Point", "coordinates": [72.5, 23]},
                    "properties": {"n": 1}}
    assert K.feature_fingerprint(from_json) == K.feature_fingerprint(from_browser)
    merged = K.merge_feature_collections([
        {"type": "FeatureCollection", "features": [from_json]},
        {"type": "FeatureCollection", "features": [from_browser]},
    ])
    assert len(merged["features"]) == 1


def test_fingerprint_keeps_booleans_distinct_from_numbers():
    a = {"geometry": None, "properties": {"flag": True}}
    b = {"geometry": None, "properties": {"flag": 1}}
    assert K.feature_fingerprint(a) != K.feature_fingerprint(b)


def test_dedupe_collections_drops_repeat_layers():
    a = {"type": "Feature", "properties": {"n": 1},
         "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}
    b = {"type": "Feature", "properties": {"n": 2},
         "geometry": {"type": "Point", "coordinates": [72.6, 23.1]}}
    out = K.dedupe_collections([
        {"type": "FeatureCollection", "features": [a]},
        {"type": "FeatureCollection", "features": [b]},
        {"type": "FeatureCollection", "features": [dict(a)]},   # probe copy of layer 1
    ])
    assert len(out) == 2                       # the duplicate layer is dropped entirely
    assert [f["properties"]["n"] for fc in out for f in fc["features"]] == [1, 2]


def test_dedupe_collections_keeps_partial_overlap():
    a = {"type": "Feature", "properties": {"n": 1},
         "geometry": {"type": "Point", "coordinates": [72.5, 23.0]}}
    b = {"type": "Feature", "properties": {"n": 2},
         "geometry": {"type": "Point", "coordinates": [72.6, 23.1]}}
    out = K.dedupe_collections([
        {"type": "FeatureCollection", "features": [a]},
        {"type": "FeatureCollection", "features": [a, b]},
    ])
    assert len(out) == 2 and len(out[1]["features"]) == 1
