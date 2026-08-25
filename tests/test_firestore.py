"""Firestore's typed-value encoding."""

from tpmap.coerce import (coerce_to_feature_collection, looks_like_firestore,
                          unwrap_firestore)


def test_scalars_are_unwrapped():
    assert unwrap_firestore({"stringValue": "x"}) == "x"
    assert unwrap_firestore({"doubleValue": 23.5}) == 23.5
    assert unwrap_firestore({"integerValue": "7"}) == 7
    assert unwrap_firestore({"booleanValue": True}) is True
    assert unwrap_firestore({"nullValue": None}) is None


def test_arrays_and_maps():
    assert unwrap_firestore({"arrayValue": {"values": [
        {"doubleValue": 1.0}, {"doubleValue": 2.0}]}}) == [1.0, 2.0]
    assert unwrap_firestore({"mapValue": {"fields": {
        "a": {"stringValue": "b"}}}}) == {"a": "b"}


def test_geopoint_becomes_a_latlng_object():
    assert unwrap_firestore({"geoPointValue": {"latitude": 23.0, "longitude": 72.5}}) \
        == {"lat": 23.0, "lng": 72.5}


def test_document_fields_are_flattened_with_the_id_kept():
    doc = {"name": "projects/p/databases/(default)/documents/plots/FP-1",
           "fields": {"zone": {"stringValue": "Residential"}}}
    assert unwrap_firestore(doc) == {"zone": "Residential", "_id": "FP-1"}


def test_detection():
    assert looks_like_firestore({"documents": [{"fields": {"a": {"stringValue": "b"}}}]})
    assert not looks_like_firestore({"plots": [{"lat": 23.0, "lng": 72.5}]})


def test_a_firestore_response_converts_to_features():
    """The shape townplanmap's Firebase backend would plausibly return."""
    payload = {"documents": [
        {"name": "projects/townplanmap/databases/(default)/documents/plots/FP-1",
         "fields": {
             "fp_no": {"stringValue": "FP-1"},
             "zone": {"stringValue": "Residential"},
             "area_sqm": {"integerValue": "1450"},
             "boundary": {"arrayValue": {"values": [
                 {"geoPointValue": {"latitude": 23.00, "longitude": 72.50}},
                 {"geoPointValue": {"latitude": 23.00, "longitude": 72.51}},
                 {"geoPointValue": {"latitude": 23.01, "longitude": 72.51}},
                 {"geoPointValue": {"latitude": 23.00, "longitude": 72.50}},
             ]}}}}]}
    fc = coerce_to_feature_collection(payload)
    assert len(fc["features"]) == 1
    feat = fc["features"][0]
    assert feat["geometry"]["type"] == "Polygon"
    assert feat["geometry"]["coordinates"][0][0] == [72.50, 23.00]
    assert feat["properties"]["fp_no"] == "FP-1"
    assert feat["properties"]["area_sqm"] == 1450


def test_unwrapping_is_idempotent():
    plain = {"plots": [{"lat": 23.0, "lng": 72.5}]}
    assert unwrap_firestore(plain) == plain
