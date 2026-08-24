"""Geometry conversion and KML serialisation.

The site may hand us geodata in any of several shapes (raw KML, KMZ, GeoJSON,
Esri REST JSON).  Everything is normalised to GeoJSON internally and written
out as KML from here, so the rest of the scraper never has to care which
format a particular endpoint happened to use.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import zipfile
from io import BytesIO
from xml.sax.saxutils import escape

# Web Mercator half-circumference, for the 3857 -> 4326 unprojection.
_MERC_R = 20037508.342789244

# wkids that mean "already WGS84 lon/lat"
_WGS84_WKIDS = {4326, 4269}
_MERCATOR_WKIDS = {3857, 102100, 900913, 102113}


class ConversionError(Exception):
    """Raised when a payload cannot be coerced into GeoJSON."""


# --------------------------------------------------------------------------
# projection
# --------------------------------------------------------------------------

def mercator_to_wgs84(x: float, y: float) -> tuple[float, float]:
    """Unproject a Web Mercator (EPSG:3857) coordinate to lon/lat degrees."""
    lon = x / _MERC_R * 180.0
    lat = y / _MERC_R * 180.0
    lat = 180.0 / math.pi * (2.0 * math.atan(math.exp(lat * math.pi / 180.0)) - math.pi / 2.0)
    return lon, lat


def _looks_projected(x: float, y: float) -> bool:
    """True if a coordinate pair is clearly not degrees."""
    return abs(x) > 180.0 or abs(y) > 90.0


# --------------------------------------------------------------------------
# Esri REST JSON -> GeoJSON
# --------------------------------------------------------------------------

def _ring_area(ring: list) -> float:
    """Signed shoelace area.  Negative == clockwise == Esri outer ring."""
    total = 0.0
    for i in range(len(ring) - 1):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[i + 1][0], ring[i + 1][1]
        total += (x2 - x1) * (y2 + y1)
    return total


def esri_to_geojson(payload: dict) -> dict:
    """Convert an Esri FeatureSet (ArcGIS REST ``f=json``) to a FeatureCollection."""
    features = payload.get("features")
    if features is None:
        raise ConversionError("no 'features' key in Esri payload")

    wkid = 4326
    sr = payload.get("spatialReference") or {}
    if isinstance(sr, dict):
        wkid = sr.get("latestWkid") or sr.get("wkid") or 4326

    def fix(pt):
        x, y = float(pt[0]), float(pt[1])
        if wkid in _MERCATOR_WKIDS or (wkid not in _WGS84_WKIDS and _looks_projected(x, y)):
            return list(mercator_to_wgs84(x, y))
        return [x, y]

    out = []
    for feat in features:
        geom = feat.get("geometry") or {}
        attrs = feat.get("attributes") or {}
        gj = None

        if "x" in geom and "y" in geom:
            if geom["x"] is not None and geom["y"] is not None:
                gj = {"type": "Point", "coordinates": fix([geom["x"], geom["y"]])}
        elif "points" in geom:
            gj = {"type": "MultiPoint", "coordinates": [fix(p) for p in geom["points"]]}
        elif "paths" in geom:
            paths = [[fix(p) for p in path] for path in geom["paths"]]
            gj = ({"type": "LineString", "coordinates": paths[0]} if len(paths) == 1
                  else {"type": "MultiLineString", "coordinates": paths})
        elif "rings" in geom:
            outers, holes = [], []
            for ring in geom["rings"]:
                (holes if _ring_area(ring) > 0 else outers).append([fix(p) for p in ring])
            if not outers:  # every ring read as a hole; treat them all as outers
                outers, holes = [[fix(p) for p in r] for r in geom["rings"]], []
            # Esri does not say which hole belongs to which outer ring; with a
            # single outer ring the answer is unambiguous, otherwise drop them
            # rather than attach them to the wrong polygon.
            if len(outers) == 1:
                gj = {"type": "Polygon", "coordinates": [outers[0]] + holes}
            else:
                gj = {"type": "MultiPolygon", "coordinates": [[o] for o in outers]}

        if gj is None:
            continue
        out.append({"type": "Feature", "geometry": gj, "properties": attrs})

    return {"type": "FeatureCollection", "features": out}


# --------------------------------------------------------------------------
# sniffing
# --------------------------------------------------------------------------

def looks_like_geojson(obj) -> bool:
    if not isinstance(obj, dict):
        return False
    t = obj.get("type")
    if t in ("FeatureCollection", "Feature", "GeometryCollection"):
        return True
    return t in ("Point", "MultiPoint", "LineString", "MultiLineString",
                 "Polygon", "MultiPolygon") and "coordinates" in obj


def looks_like_esri(obj) -> bool:
    if not isinstance(obj, dict) or not isinstance(obj.get("features"), list):
        return False
    for feat in obj["features"]:
        if isinstance(feat, dict) and ("attributes" in feat or "geometry" in feat):
            geom = feat.get("geometry")
            if isinstance(geom, dict) and any(
                k in geom for k in ("rings", "paths", "points", "x")
            ):
                return True
    return False


def to_feature_collection(obj) -> dict:
    """Normalise any recognised JSON geo payload into a FeatureCollection."""
    if looks_like_esri(obj):
        return esri_to_geojson(obj)
    if not looks_like_geojson(obj):
        raise ConversionError("payload is neither GeoJSON nor Esri JSON")

    t = obj["type"]
    if t == "FeatureCollection":
        return obj
    if t == "Feature":
        return {"type": "FeatureCollection", "features": [obj]}
    if t == "GeometryCollection":
        return {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": g, "properties": {}}
            for g in obj.get("geometries", [])
        ]}
    return {"type": "FeatureCollection",
            "features": [{"type": "Feature", "geometry": obj, "properties": {}}]}


# --------------------------------------------------------------------------
# KML output
# --------------------------------------------------------------------------

_DEFAULT_STYLE = """  <Style id="tpmap">
    <LineStyle><color>ff0000ff</color><width>2</width></LineStyle>
    <PolyStyle><color>400000ff</color></PolyStyle>
    <IconStyle><scale>0.9</scale></IconStyle>
  </Style>"""

# Attribute keys worth promoting to the placemark <name>, best first.
_NAME_KEYS = ("name", "Name", "NAME", "title", "label", "LABEL",
              "fp_no", "FP_NO", "final_plot", "plot_no", "PLOT_NO",
              "survey_no", "SURVEY_NO", "tp_no", "TPNO", "id", "OBJECTID")


def _coord(pos) -> str:
    if len(pos) >= 3:
        return f"{float(pos[0]):.8g},{float(pos[1]):.8g},{float(pos[2]):.8g}"
    return f"{float(pos[0]):.8g},{float(pos[1]):.8g}"


def _coords(seq) -> str:
    return " ".join(_coord(p) for p in seq)


def _ring(seq, tag: str) -> str:
    return f"<{tag}><LinearRing><coordinates>{_coords(seq)}</coordinates></LinearRing></{tag}>"


def _polygon(rings) -> str:
    if not rings:
        return ""
    parts = [_ring(rings[0], "outerBoundaryIs")]
    parts += [_ring(r, "innerBoundaryIs") for r in rings[1:]]
    return "<Polygon>" + "".join(parts) + "</Polygon>"


def geometry_to_kml(geom) -> str:
    """Serialise a single GeoJSON geometry as a KML geometry element."""
    if not isinstance(geom, dict):
        return ""
    t, c = geom.get("type"), geom.get("coordinates")
    if t == "GeometryCollection":
        inner = "".join(geometry_to_kml(g) for g in geom.get("geometries", []))
        return f"<MultiGeometry>{inner}</MultiGeometry>" if inner else ""
    if c is None:
        return ""

    if t == "Point":
        return f"<Point><coordinates>{_coord(c)}</coordinates></Point>"
    if t == "MultiPoint":
        return "<MultiGeometry>" + "".join(
            f"<Point><coordinates>{_coord(p)}</coordinates></Point>" for p in c
        ) + "</MultiGeometry>"
    if t == "LineString":
        return f"<LineString><coordinates>{_coords(c)}</coordinates></LineString>"
    if t == "MultiLineString":
        return "<MultiGeometry>" + "".join(
            f"<LineString><coordinates>{_coords(l)}</coordinates></LineString>" for l in c
        ) + "</MultiGeometry>"
    if t == "Polygon":
        return _polygon(c)
    if t == "MultiPolygon":
        return "<MultiGeometry>" + "".join(_polygon(p) for p in c) + "</MultiGeometry>"
    return ""


def _placemark(feature: dict, fallback_name: str) -> str:
    props = feature.get("properties") or {}
    geom_xml = geometry_to_kml(feature.get("geometry"))
    if not geom_xml:
        return ""

    name = fallback_name
    for key in _NAME_KEYS:
        val = props.get(key)
        if val not in (None, ""):
            name = str(val)
            break

    rows = "".join(
        f"<tr><td>{escape(str(k))}</td><td>{escape(str(v))}</td></tr>"
        for k, v in props.items() if v not in (None, "")
    )
    data = "".join(
        f'<Data name="{escape(str(k), {chr(34): "&quot;"})}">'
        f"<value>{escape(str(v))}</value></Data>"
        for k, v in props.items() if v not in (None, "")
    )

    out = [f"    <Placemark><name>{escape(name)}</name>"]
    if rows:
        out.append(f"<description><![CDATA[<table>{rows}</table>]]></description>")
    out.append("<styleUrl>#tpmap</styleUrl>")
    if data:
        out.append(f"<ExtendedData>{data}</ExtendedData>")
    out.append(geom_xml)
    out.append("</Placemark>")
    return "".join(out)


def feature_collection_to_kml(fc: dict, doc_name: str = "TownPlanMap export") -> str:
    """Render a FeatureCollection as a complete KML document."""
    placemarks = []
    for i, feat in enumerate(fc.get("features", []), start=1):
        pm = _placemark(feat, f"Feature {i}")
        if pm:
            placemarks.append(pm)

    body = "\n".join(placemarks)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<kml xmlns="http://www.opengis.net/kml/2.2">\n'
        "  <Document>\n"
        f"    <name>{escape(doc_name)}</name>\n"
        f"{_DEFAULT_STYLE}\n"
        f"{body}\n"
        "  </Document>\n"
        "</kml>\n"
    )


def _normalise_numbers(obj):
    """Coerce every number to a rounded float.

    The same coordinate reaches us as ``23.0`` when parsed from JSON and as
    ``23`` when handed back by the browser (JavaScript has one number type, so
    integral values deserialise as Python ints).  Without this the two spellings
    of one feature would hash differently and survive deduplication.
    """
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, (int, float)):
        return round(float(obj), 9)
    if isinstance(obj, list):
        return [_normalise_numbers(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _normalise_numbers(v) for k, v in obj.items()}
    return obj


def feature_fingerprint(feature: dict) -> str:
    """Stable hash of a feature's geometry and properties."""
    payload = json.dumps(
        _normalise_numbers([feature.get("geometry"), feature.get("properties") or {}]),
        sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def dedupe_collections(collections) -> list[dict]:
    """Drop features already seen in an earlier collection, and empty leftovers.

    Used when writing one file per layer: without this, a layer captured over
    the network and again from the live map object would be written twice.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for fc in collections:
        kept = []
        for feat in fc.get("features", []):
            fp = feature_fingerprint(feat)
            if fp in seen:
                continue
            seen.add(fp)
            kept.append(feat)
        if kept:
            out.append({"type": "FeatureCollection", "features": kept})
    return out


def merge_feature_collections(collections, dedupe: bool = True) -> dict:
    """Flatten several FeatureCollections into one.

    Discovery channels overlap by design -- a layer fetched over the network is
    usually also sitting parsed in the live map object -- so identical features
    are collapsed unless the caller asks otherwise.
    """
    feats: list[dict] = []
    seen: set[str] = set()
    for fc in collections:
        for feat in fc.get("features", []):
            if dedupe:
                fp = feature_fingerprint(feat)
                if fp in seen:
                    continue
                seen.add(fp)
            feats.append(feat)
    return {"type": "FeatureCollection", "features": feats}


# --------------------------------------------------------------------------
# KMZ
# --------------------------------------------------------------------------

def kmz_to_kml(blob: bytes) -> bytes:
    """Pull the primary KML document out of a KMZ archive."""
    with zipfile.ZipFile(BytesIO(blob)) as zf:
        names = [n for n in zf.namelist() if n.lower().endswith(".kml")]
        if not names:
            raise ConversionError("KMZ contains no .kml entry")
        # The spec says the root doc.kml wins; otherwise take the shallowest.
        names.sort(key=lambda n: (n.lower() != "doc.kml", n.count("/"), len(n)))
        return zf.read(names[0])


def kml_to_kmz(kml_text: str, inner_name: str = "doc.kml") -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(inner_name, kml_text)
    return buf.getvalue()


def is_kml_bytes(blob: bytes) -> bool:
    head = blob[:4096].lstrip()
    return head.startswith(b"<kml") or (head.startswith(b"<?xml") and b"<kml" in blob[:8192])


def is_kmz_bytes(blob: bytes) -> bool:
    return blob[:2] == b"PK"


def count_placemarks(blob: bytes) -> int:
    try:
        return len(re.findall(rb"<Placemark[\s>]", blob))
    except Exception:
        return 0
