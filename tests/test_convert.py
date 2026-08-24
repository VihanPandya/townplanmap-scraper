"""The offline `tpmap convert` command."""

import json
import xml.etree.ElementTree as ET

from tpmap.cli import main
from tpmap.kml import kml_to_kmz

NS = {"k": "http://www.opengis.net/kml/2.2"}

RING = [[72.50, 23.00], [72.51, 23.00], [72.51, 23.01], [72.50, 23.00]]
ENVELOPE = {"status": "success", "data": {"type": "FeatureCollection", "features": [
    {"type": "Feature", "properties": {"fp_no": "FP-1"},
     "geometry": {"type": "Polygon", "coordinates": [RING]}}]}}
LATLNG = {"result": {"plots": [{"fp_no": "FP-7", "boundary": [
    {"lat": 23.00, "lng": 72.50}, {"lat": 23.00, "lng": 72.51},
    {"lat": 23.01, "lng": 72.51}, {"lat": 23.00, "lng": 72.50}]}]}}


def write(tmp_path, name, obj):
    p = tmp_path / name
    p.write_text(json.dumps(obj))
    return p


def names(path):
    return [e.text for e in ET.parse(path).getroot().findall(".//k:Placemark/k:name", NS)]


def test_converts_beside_the_input_by_default(tmp_path):
    src = write(tmp_path, "envelope.json", ENVELOPE)
    assert main(["convert", str(src)]) == 0
    assert names(tmp_path / "envelope.kml") == ["FP-1"]


def test_named_output(tmp_path):
    src = write(tmp_path, "a.json", ENVELOPE)
    out = tmp_path / "out.kml"
    assert main(["convert", str(src), "-o", str(out)]) == 0
    assert out.exists()


def test_merges_multiple_inputs(tmp_path):
    a = write(tmp_path, "a.json", ENVELOPE)
    b = write(tmp_path, "b.json", LATLNG)
    out = tmp_path / "merged.kml"
    assert main(["convert", str(a), str(b), "-o", str(out)]) == 0
    assert names(out) == ["FP-1", "FP-7"]


def test_split_keeps_inputs_separate(tmp_path):
    a = write(tmp_path, "a.json", ENVELOPE)
    b = write(tmp_path, "b.json", LATLNG)
    assert main(["convert", str(a), str(b), "-o", str(tmp_path / "x.kml"), "--split"]) == 0
    assert names(tmp_path / "a.kml") == ["FP-1"]
    assert names(tmp_path / "b.kml") == ["FP-7"]


def test_latlon_flag(tmp_path):
    src = write(tmp_path, "p.json", {"pts": {"coords": [[23.0, 72.5], [23.1, 72.6]]}})
    assert main(["convert", str(src), "--latlon"]) == 0
    coords = ET.parse(tmp_path / "p.kml").getroot().find(
        ".//k:LineString/k:coordinates", NS).text
    assert coords.split()[0] == "72.5,23"


def test_kmz_is_unwrapped(tmp_path):
    inner = ('<?xml version="1.0" encoding="UTF-8"?><kml xmlns="http://www.opengis.net/kml/2.2">'
             "<Document><Placemark><name>P</name>"
             "<Point><coordinates>72.5,23</coordinates></Point></Placemark></Document></kml>")
    src = tmp_path / "z.kmz"
    src.write_bytes(kml_to_kmz(inner))
    assert main(["convert", str(src)]) == 0
    assert names(tmp_path / "z.kml") == ["P"]


def test_existing_kml_is_left_alone(tmp_path, capsys):
    src = tmp_path / "already.kml"
    src.write_text('<?xml version="1.0"?><kml xmlns="http://www.opengis.net/kml/2.2"/>')
    assert main(["convert", str(src)]) == 1          # nothing to write
    assert "already KML" in capsys.readouterr().out


def test_missing_file_is_reported(tmp_path):
    assert main(["convert", str(tmp_path / "nope.json")]) == 2


def test_invalid_json_is_reported(tmp_path, capsys):
    src = tmp_path / "bad.json"
    src.write_text("{not json")
    assert main(["convert", str(src)]) == 1
    assert "not valid JSON" in capsys.readouterr().err


def test_a_discovery_report_gets_a_pointed_message(tmp_path, capsys):
    """Users reach for the only .json fetch produced; say why it is not data."""
    src = write(tmp_path, "report.json", {
        "page_url": "https://townplanmap.com/tp/x",
        "hits": [{"url": "https://townplanmap.com/a.kml", "kind": "kml"}],
        "inline_layers": 0})
    assert main(["convert", str(src)]) == 1
    err = capsys.readouterr().err
    assert "discovery report" in err and "fetch one of those URLs" in err


def test_payload_with_no_geometry_fails_cleanly(tmp_path, capsys):
    src = write(tmp_path, "plain.json", {"status": "ok", "count": 3})
    assert main(["convert", str(src)]) == 1
    assert "no geometry found" in capsys.readouterr().err


def test_stdin(tmp_path, monkeypatch, capsys):
    import io
    import sys
    data = json.dumps(ENVELOPE).encode()
    monkeypatch.setattr(sys, "stdin", type("S", (), {"buffer": io.BytesIO(data)})())
    out = tmp_path / "from-stdin.kml"
    assert main(["convert", "-", "-o", str(out)]) == 0
    assert names(out) == ["FP-1"]
