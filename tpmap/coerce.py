"""Pulling geodata out of JSON that is not already GeoJSON.

Map back-ends rarely serve clean GeoJSON.  The geometry usually arrives wrapped
in an API envelope, keyed by a name of the site's own choosing, and expressed in
whatever coordinate shape the front-end happened to want -- lat/lng objects, WKT
from a PostGIS column, an encoded polyline.  This module digs through an
arbitrary JSON document and reconstructs GeoJSON features from any of those.
"""

from __future__ import annotations

import logging
import re

from .kml import (ConversionError, esri_to_geojson, feature_fingerprint,
                  looks_like_esri, looks_like_geojson, to_feature_collection)

log = logging.getLogger("tpmap.coerce")

MAX_DEPTH = 12
MAX_NODES = 200_000

GEOJSON_GEOM_TYPES = {"Point", "MultiPoint", "LineString", "MultiLineString",
                      "Polygon", "MultiPolygon", "GeometryCollection"}

# Keys that plausibly hold geometry, checked before a generic scan.
GEOM_KEY_RE = re.compile(
    r"^(geo|geom|geometry|shape|the_geom|wkt|boundary|bounds|outline|coords?|"
    r"coordinates|points?|path|paths|polygon|ring|rings|latlngs?|latlons?|"
    r"polyline|encoded|encodedpath)$", re.I)
POLYLINE_KEY_RE = re.compile(r"(polyline|encoded)", re.I)

LAT_KEYS = ("lat", "latitude", "y", "Lat", "LAT", "Latitude")
LON_KEYS = ("lng", "lon", "long", "longitude", "x", "Lng", "Lon", "LON", "Longitude")
# For deciding that a whole record *is* a point, only unambiguous names count:
# a bare x/y pair is as likely to be a pixel offset as a coordinate.
STRICT_LAT_KEYS = ("lat", "latitude", "Lat", "LAT", "Latitude")
STRICT_LON_KEYS = ("lng", "lon", "long", "longitude", "Lng", "Lon", "LON", "Longitude")

WKT_RE = re.compile(
    r"^\s*(POINT|LINESTRING|POLYGON|MULTIPOINT|MULTILINESTRING|MULTIPOLYGON)\s*(Z|M|ZM)?\s*\(",
    re.I)
POLYLINE_CHARS_RE = re.compile(r"^[\x3f-\x7e\\\\]{8,}$")


# --------------------------------------------------------------------------
# coordinate helpers
# --------------------------------------------------------------------------

def _num(v):
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip())
        except ValueError:
            return None
    return None


def _plausible(lon, lat) -> bool:
    return lon is not None and lat is not None and abs(lon) <= 180 and abs(lat) <= 90


def _pair_from_dict(d: dict, latlon: bool = False):
    """Read a {lat: .., lng: ..} style object as a [lon, lat] pair."""
    lat = next((_num(d[k]) for k in LAT_KEYS if k in d), None)
    lon = next((_num(d[k]) for k in LON_KEYS if k in d), None)
    if _plausible(lon, lat):
        return [lon, lat]
    return None


def _pair_from_list(v, latlon: bool = False):
    """Read a two-number array as a coordinate pair."""
    if not isinstance(v, (list, tuple)) or not 2 <= len(v) <= 4:
        return None
    a, b = _num(v[0]), _num(v[1])
    if a is None or b is None:
        return None
    lon, lat = (b, a) if latlon else (a, b)
    if _plausible(lon, lat):
        return [lon, lat]
    # A pair that only parses the other way round almost certainly is that way.
    if _plausible(lat, lon):
        return [lat, lon]
    return None


def coord_list(seq, latlon: bool = False):
    """Convert a sequence of points, in whichever spelling, to [[lon, lat], ..]."""
    if not isinstance(seq, (list, tuple)) or not seq:
        return None
    out = []
    for item in seq:
        pair = _pair_from_dict(item, latlon) if isinstance(item, dict) \
            else _pair_from_list(item, latlon)
        if pair is None:
            return None
        out.append(pair)
    return out or None


def geometry_from_coords(coords):
    """Guess the geometry type a bare coordinate list represents."""
    if not coords:
        return None
    if len(coords) == 1:
        return {"type": "Point", "coordinates": coords[0]}
    if len(coords) >= 4 and coords[0] == coords[-1]:
        return {"type": "Polygon", "coordinates": [coords]}
    return {"type": "LineString", "coordinates": coords}


# --------------------------------------------------------------------------
# WKT
# --------------------------------------------------------------------------

def parse_wkt(text: str):
    """Parse a WKT geometry string into a GeoJSON geometry."""
    if not isinstance(text, str) or not WKT_RE.match(text):
        return None
    m = WKT_RE.match(text)
    kind = m.group(1).upper()
    body = text[m.end() - 1:].strip()

    def points(chunk: str):
        out = []
        for part in chunk.split(","):
            bits = part.replace("(", " ").replace(")", " ").split()
            if len(bits) < 2:
                return None
            lon, lat = _num(bits[0]), _num(bits[1])
            if not _plausible(lon, lat):
                return None
            out.append([lon, lat])
        return out

    def split_groups(chunk: str):
        """Split a parenthesised list at depth 1."""
        chunk = chunk.strip()
        if chunk.startswith("(") and chunk.endswith(")"):
            chunk = chunk[1:-1]
        groups, depth, start = [], 0, 0
        for i, ch in enumerate(chunk):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            elif ch == "," and depth == 0:
                groups.append(chunk[start:i])
                start = i + 1
        groups.append(chunk[start:])
        return [g.strip() for g in groups if g.strip()]

    try:
        if kind == "POINT":
            pts = points(body.strip("()"))
            return {"type": "Point", "coordinates": pts[0]} if pts else None
        if kind == "LINESTRING":
            pts = points(body.strip("()"))
            return {"type": "LineString", "coordinates": pts} if pts else None
        if kind == "MULTIPOINT":
            pts = points(body.strip("()").replace("(", "").replace(")", ""))
            return {"type": "MultiPoint", "coordinates": pts} if pts else None
        if kind == "POLYGON":
            rings = [points(g) for g in split_groups(body)]
            rings = [r for r in rings if r]
            return {"type": "Polygon", "coordinates": rings} if rings else None
        if kind == "MULTILINESTRING":
            lines = [points(g) for g in split_groups(body)]
            lines = [ln for ln in lines if ln]
            return {"type": "MultiLineString", "coordinates": lines} if lines else None
        if kind == "MULTIPOLYGON":
            polys = []
            for poly in split_groups(body):
                rings = [points(g) for g in split_groups(poly)]
                rings = [r for r in rings if r]
                if rings:
                    polys.append(rings)
            return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    except (AttributeError, IndexError, TypeError) as exc:
        log.debug("wkt parse failed: %s", exc)
    return None


# --------------------------------------------------------------------------
# encoded polyline
# --------------------------------------------------------------------------

def decode_polyline(text: str, precision: int = 5):
    """Decode a Google encoded polyline into [[lon, lat], ..]."""
    if not isinstance(text, str) or len(text) < 4:
        return None
    factor = float(10 ** precision)
    coords, index, lat, lng = [], 0, 0, 0
    try:
        while index < len(text):
            for target in ("lat", "lng"):
                shift = result = 0
                while True:
                    if index >= len(text):
                        return coords or None
                    b = ord(text[index]) - 63
                    index += 1
                    result |= (b & 0x1F) << shift
                    shift += 5
                    if b < 0x20:
                        break
                delta = ~(result >> 1) if result & 1 else (result >> 1)
                if target == "lat":
                    lat += delta
                else:
                    lng += delta
            lon_d, lat_d = lng / factor, lat / factor
            if not _plausible(lon_d, lat_d):
                return None
            coords.append([lon_d, lat_d])
    except (TypeError, ValueError):
        return None
    return coords or None


# --------------------------------------------------------------------------
# Firestore
# --------------------------------------------------------------------------

# Firestore's REST API wraps every scalar in a type tag, so a document reads as
# {"fields": {"lat": {"doubleValue": 23.0}}} rather than {"lat": 23.0}. Nothing
# downstream recognises that, so unwrap it back to plain JSON first.
_FIRESTORE_SCALARS = {
    "stringValue": str, "booleanValue": bool, "doubleValue": float,
    "integerValue": int, "timestampValue": str, "bytesValue": str,
    "referenceValue": str,
}


def unwrap_firestore(node, depth: int = 0):
    """Convert Firestore typed JSON into ordinary JSON. Idempotent."""
    if depth > MAX_DEPTH:
        return node

    if isinstance(node, list):
        return [unwrap_firestore(v, depth + 1) for v in node]
    if not isinstance(node, dict):
        return node

    if len(node) == 1:
        (key, value), = node.items()
        if key in _FIRESTORE_SCALARS:
            caster = _FIRESTORE_SCALARS[key]
            try:
                return caster(value)
            except (TypeError, ValueError):
                return value
        if key == "nullValue":
            return None
        if key == "arrayValue":
            values = (value or {}).get("values", []) if isinstance(value, dict) else []
            return [unwrap_firestore(v, depth + 1) for v in values]
        if key == "mapValue":
            fields = (value or {}).get("fields", {}) if isinstance(value, dict) else {}
            return {k: unwrap_firestore(v, depth + 1) for k, v in fields.items()}
        if key == "geoPointValue" and isinstance(value, dict):
            return {"lat": value.get("latitude"), "lng": value.get("longitude")}

    # A document: merge its fields up, keeping the id as a property.
    if "fields" in node and isinstance(node["fields"], dict):
        out = {k: unwrap_firestore(v, depth + 1) for k, v in node["fields"].items()}
        name = node.get("name")
        if isinstance(name, str):
            out.setdefault("_id", name.rsplit("/", 1)[-1])
        return out

    return {k: unwrap_firestore(v, depth + 1) for k, v in node.items()}


def looks_like_firestore(obj) -> bool:
    """True if the payload uses Firestore's typed-value encoding."""
    found = [False]

    def walk(node, depth=0):
        if found[0] or depth > 6:
            return
        if isinstance(node, list):
            for v in node[:50]:
                walk(v, depth + 1)
        elif isinstance(node, dict):
            for key in node:
                if key in _FIRESTORE_SCALARS or key in (
                        "mapValue", "arrayValue", "geoPointValue"):
                    found[0] = True
                    return
            for v in list(node.values())[:50]:
                walk(v, depth + 1)

    walk(obj)
    return found[0]


# --------------------------------------------------------------------------
# record -> feature
# --------------------------------------------------------------------------

def _scalar_props(record: dict) -> dict:
    return {k: v for k, v in record.items()
            if isinstance(v, (str, int, float, bool)) or v is None}


def geometry_from_value(value, key: str = "", latlon: bool = False):
    """Interpret one JSON value as a geometry, if it can be read as one."""
    if isinstance(value, dict):
        if value.get("type") in GEOJSON_GEOM_TYPES and (
                "coordinates" in value or "geometries" in value):
            return value
        pair = _pair_from_dict(value, latlon)
        if pair:
            return {"type": "Point", "coordinates": pair}
        return None

    if isinstance(value, str):
        wkt = parse_wkt(value)
        if wkt:
            return wkt
        # Only trust a polyline decode when the key says so -- the encoding
        # is printable ASCII and would otherwise match ordinary text.
        if POLYLINE_KEY_RE.search(key) and POLYLINE_CHARS_RE.match(value):
            coords = decode_polyline(value)
            if coords:
                return geometry_from_coords(coords)
        return None

    if isinstance(value, (list, tuple)) and value:
        coords = coord_list(value, latlon)
        if coords:
            return geometry_from_coords(coords)
        # a list of rings / lines
        rings = [coord_list(v, latlon) for v in value if isinstance(v, (list, tuple))]
        rings = [r for r in rings if r]
        if rings and len(rings) == len(value):
            if all(len(r) >= 4 and r[0] == r[-1] for r in rings):
                return {"type": "Polygon", "coordinates": rings}
            return {"type": "MultiLineString", "coordinates": rings}
    return None


def feature_from_record(record: dict, latlon: bool = False):
    """Build a Feature from a plain object that carries geometry somewhere."""
    if not isinstance(record, dict):
        return None

    # Prefer a conventionally named key, then fall back to any value that reads
    # as geometry -- but never treat an unnamed value as geometry unless it is
    # unambiguous, or ordinary numeric arrays would become bogus features.
    for key, value in record.items():
        if GEOM_KEY_RE.match(key) or POLYLINE_KEY_RE.search(key):
            geom = geometry_from_value(value, key, latlon)
            if geom:
                props = _scalar_props(record)
                props.pop(key, None)
                return {"type": "Feature", "geometry": geom, "properties": props}

    for key, value in record.items():
        if isinstance(value, dict) and value.get("type") in GEOJSON_GEOM_TYPES:
            props = _scalar_props(record)
            return {"type": "Feature", "geometry": value, "properties": props}

    # The record may itself be a point.
    lat = next((_num(record[k]) for k in STRICT_LAT_KEYS if k in record), None)
    lon = next((_num(record[k]) for k in STRICT_LON_KEYS if k in record), None)
    if latlon and lat is not None and lon is not None:
        lat, lon = lon, lat
    if _plausible(lon, lat):
        props = {k: v for k, v in _scalar_props(record).items()
                 if k not in STRICT_LAT_KEYS + STRICT_LON_KEYS}
        return {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]},
                "properties": props}
    return None


# --------------------------------------------------------------------------
# recursive scan
# --------------------------------------------------------------------------

def _collect(node, out: list, latlon: bool, depth: int = 0, budget: list | None = None):
    if budget is None:
        budget = [MAX_NODES]
    if depth > MAX_DEPTH or budget[0] <= 0:
        return
    budget[0] -= 1

    if isinstance(node, dict):
        # A recognised container short-circuits: take it whole, do not descend.
        if looks_like_geojson(node):
            try:
                out.extend(to_feature_collection(node).get("features", []))
                return
            except ConversionError:
                pass
        if looks_like_esri(node):
            try:
                out.extend(esri_to_geojson(node).get("features", []))
                return
            except ConversionError:
                pass

        feat = feature_from_record(node, latlon)
        if feat:
            out.append(feat)
            return

        for value in node.values():
            _collect(value, out, latlon, depth + 1, budget)

    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect(item, out, latlon, depth + 1, budget)


def coerce_to_feature_collection(obj, *, latlon: bool = False) -> dict:
    """Best effort: get a FeatureCollection out of any JSON document.

    Tries a straight GeoJSON/Esri read first, then falls back to searching the
    document for anything shaped like geometry.
    """
    try:
        fc = to_feature_collection(obj)
        if fc.get("features"):
            return fc
    except ConversionError:
        pass

    if looks_like_firestore(obj):
        obj = unwrap_firestore(obj)
        log.debug("unwrapped Firestore typed values")

    found: list = []
    _collect(obj, found, latlon)

    seen, deduped = set(), []
    for feat in found:
        fp = feature_fingerprint(feat)
        if fp in seen:
            continue
        seen.add(fp)
        deduped.append(feat)

    if not deduped:
        raise ConversionError("no geometry found anywhere in this payload")
    return {"type": "FeatureCollection", "features": deduped}


def contains_geodata(obj) -> bool:
    """Cheap check used to classify a response without fully converting it."""
    try:
        coerce_to_feature_collection(obj)
        return True
    except ConversionError:
        return False
