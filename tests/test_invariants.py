"""One known-good and one known-bad case per invariant, with explicit severity.

All fixtures are local; no network access is needed.
"""
import json

import pytest
import rasterio

from stac_integrity.audit import audit_item, audit_item_dict
from stac_integrity.collection import audit_collection
from test_audit import make_fixture, write_item


def severities(result):
    return {(f.code, f.severity) for f in result.findings}


# (name, mutate(asset), expected (code, severity))
KNOWN_BAD = [
    ("crs", lambda a: a.update({"proj:code": "EPSG:4326"}), ("CRS_MISMATCH", "ERROR")),
    ("shape", lambda a: a.update({"proj:shape": [4, 3]}), ("SHAPE_MISMATCH", "ERROR")),
    ("transform", lambda a: a["proj:transform"].__setitem__(0, 20), ("TRANSFORM_MISMATCH", "ERROR")),
    ("bbox", lambda a: a["proj:bbox"].__setitem__(2, 999), ("BBOX_MISMATCH", "ERROR")),
    ("band_count", lambda a: a["raster:bands"].pop(), ("BAND_COUNT_MISMATCH", "ERROR")),
    ("dtype", lambda a: a["raster:bands"][0].update({"data_type": "int8"}), ("DATA_TYPE_MISMATCH", "ERROR")),
    ("nodata", lambda a: a["raster:bands"][0].update({"nodata": 7}), ("NODATA_MISMATCH", "ERROR")),
    ("scale", lambda a: a["raster:bands"][0].update({"scale": 0.001}), ("SCALE_MISMATCH", "WARN")),
    ("offset", lambda a: a["raster:bands"][0].update({"offset": -0.1}), ("OFFSET_MISMATCH", "WARN")),
]


def test_known_good_fixture_has_no_findings(tmp_path):
    item_path, _ = make_fixture(tmp_path)
    result = audit_item(str(item_path))
    assert result.findings == []
    assert result.ok
    assert result.checked_assets == 1


@pytest.mark.parametrize("name,mutate,expected", KNOWN_BAD, ids=[k[0] for k in KNOWN_BAD])
def test_known_bad_invariant_reports_expected_severity(tmp_path, name, mutate, expected):
    item_path, item = make_fixture(tmp_path)
    mutate(item["assets"]["data"])
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert expected in severities(result)
    # Only the mutated invariant fires.
    assert {code for code, _ in severities(result)} == {expected[0]}
    assert result.ok is (expected[1] == "WARN")


@pytest.mark.parametrize("declared", ["EPSG:32632", 32632, 32632.0])
def test_crs_match_in_all_declared_forms(tmp_path, declared):
    item_path, item = make_fixture(tmp_path)
    asset = item["assets"]["data"]
    del asset["proj:code"]
    if isinstance(declared, str):
        asset["proj:code"] = declared
    else:
        asset["proj:epsg"] = declared
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert result.findings == []


@pytest.mark.parametrize("bad_epsg", [32632.5, 999999, "not-a-code"])
def test_unusable_epsg_is_warning_not_error(tmp_path, bad_epsg):
    # Non-integral floats and unknown codes cannot be compared. That is a
    # metadata-quality warning, never a CRS mismatch ERROR.
    item_path, item = make_fixture(tmp_path)
    asset = item["assets"]["data"]
    del asset["proj:code"]
    asset["proj:epsg"] = bad_epsg
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert severities(result) == {("CRS_UNPARSEABLE", "WARN")}
    assert result.ok


def test_auth_required_remote_asset_is_warning_not_semantic_error(monkeypatch):
    def denied(*args, **kwargs):
        raise rasterio.errors.RasterioIOError("HTTP response code: 403 - Access Denied")

    monkeypatch.setattr(rasterio, "open", denied)
    item = {
        "type": "Feature",
        "id": "needs-auth",
        "properties": {"proj:code": "EPSG:4326", "proj:shape": [1, 1]},
        "assets": {"data": {"href": "https://example.org/private.tif", "type": "image/tiff", "roles": ["data"]}},
    }
    result = audit_item_dict(item, source="https://example.org/item.json")
    assert severities(result) == {("ASSET_UNREADABLE", "WARN")}
    assert result.ok


def test_collection_level_asset_known_good(tmp_path):
    item_path, item = make_fixture(tmp_path)
    asset = dict(item["assets"]["data"])
    asset["bands"] = asset.pop("raster:bands")
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": "good-collection",
        "description": "fixture",
        "license": "MIT",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}, "temporal": {"interval": [[None, None]]}},
        "assets": {"data": asset},
        "links": [],
    }
    cp = tmp_path / "collection.json"
    cp.write_text(json.dumps(collection))
    result = audit_collection(str(cp), workers=1)
    assert result.ok
    assert result.collection_asset_result.findings == []
    assert result.checked_assets == 1
