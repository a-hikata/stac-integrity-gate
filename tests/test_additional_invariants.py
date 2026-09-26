"""Track G invariants: known-good / known-bad fixtures with explicit severity,
plus false-positive guards. All offline.
"""
import io
import json
import urllib.request
from email.message import Message

import numpy as np
import pytest
import rasterio
from rasterio.enums import ColorInterp
from rasterio.transform import from_origin

from stac_integrity.audit import audit_item, audit_item_dict
from stac_integrity.cli import main
from test_audit import make_fixture, write_item


def severities(result):
    return {(f.code, f.severity) for f in result.findings}


def _eo_only(item, n):
    """Replace raster:bands with asset-level eo:bands of length n."""
    asset = item["assets"]["data"]
    del asset["raster:bands"]
    asset["eo:bands"] = [{"name": f"B{i}", "common_name": "red"} for i in range(n)]
    return asset


# --- G1: asset-level eo:bands ------------------------------------------------

def test_eo_bands_known_good(tmp_path):
    item_path, item = make_fixture(tmp_path)  # 2-band raster
    _eo_only(item, 2)
    write_item(item_path, item)
    assert audit_item(str(item_path)).findings == []


def test_eo_bands_count_mismatch_is_warn(tmp_path):
    item_path, item = make_fixture(tmp_path)
    _eo_only(item, 3)
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert severities(result) == {("EO_BAND_COUNT_MISMATCH", "WARN")}
    assert result.ok


def test_item_level_eo_bands_never_compared_per_asset(tmp_path):
    # Item properties eo:bands lists bands across ALL assets (e.g. 13 for S2).
    item_path, item = make_fixture(tmp_path)
    del item["assets"]["data"]["raster:bands"]
    item["properties"]["eo:bands"] = [{"name": f"B{i}"} for i in range(13)]
    write_item(item_path, item)
    assert audit_item(str(item_path)).findings == []


def test_eo_bands_ignored_when_raster_bands_present(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["eo:bands"] = [{"name": "B1"}]  # raster:bands (2) is authoritative
    write_item(item_path, item)
    assert audit_item(str(item_path)).findings == []


def test_eo_bands_ignores_trailing_alpha_band(tmp_path):
    tif = tmp_path / "visual.tif"
    with rasterio.open(tif, "w", driver="GTiff", width=4, height=3, count=4, dtype="uint8",
                       crs="EPSG:32632", transform=from_origin(100, 200, 10, 10)) as dst:
        dst.write(np.ones((4, 3, 4), dtype="uint8"))
        dst.colorinterp = [ColorInterp.red, ColorInterp.green, ColorInterp.blue, ColorInterp.alpha]
    item = {"type": "Feature", "id": "rgba", "properties": {}, "assets": {"visual": {
        "href": "visual.tif", "type": "image/tiff; application=geotiff", "roles": ["data"],
        "eo:bands": [{"name": "B04"}, {"name": "B03"}, {"name": "B02"}]}}}
    p = tmp_path / "item.json"
    p.write_text(json.dumps(item))
    assert audit_item(str(p)).findings == []


# --- G2: file:size (opt-in) --------------------------------------------------

def test_file_size_known_good_local(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["file:size"] = (tmp_path / "asset.tif").stat().st_size
    write_item(item_path, item)
    assert audit_item(str(item_path), check_file_size=True).findings == []


def test_file_size_mismatch_local_is_warn(tmp_path):
    item_path, item = make_fixture(tmp_path)
    actual = (tmp_path / "asset.tif").stat().st_size
    item["assets"]["data"]["file:size"] = actual + 1
    write_item(item_path, item)
    result = audit_item(str(item_path), check_file_size=True)
    assert severities(result) == {("FILE_SIZE_MISMATCH", "WARN")}
    assert result.findings[0].actual == actual
    assert result.ok


def test_file_size_is_off_by_default(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["file:size"] = 1
    write_item(item_path, item)
    assert audit_item(str(item_path)).findings == []


@pytest.mark.parametrize("bad", ["123", -1, 1.5, True, None])
def test_file_size_unusable_declaration_is_skipped(tmp_path, bad):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["file:size"] = bad
    write_item(item_path, item)
    assert audit_item(str(item_path), check_file_size=True).findings == []


def test_file_size_cli_flag(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["file:size"] = 1
    write_item(item_path, item)
    assert main(["item", str(item_path), "--check-file-size"]) == 0
    assert main(["item", str(item_path), "--check-file-size", "--strict"]) == 1
    assert main(["item", str(item_path), "--strict"]) == 0


class _FakeResponse:
    def __init__(self, status, headers):
        self.status = status
        self.headers = Message()
        for k, v in headers.items():
            self.headers[k] = v

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _remote_item(size):
    return {"type": "Feature", "id": "remote", "properties": {}, "assets": {"data": {
        "href": "https://example.org/a.tif", "type": "image/tiff; application=geotiff",
        "roles": ["data"], "file:size": size}}}


def _patch_remote(monkeypatch, response):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["range"] = req.get_header("Range")
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    def fake_open(*a, **k):
        raise rasterio.errors.RasterioIOError("offline test")

    monkeypatch.setattr(rasterio, "open", fake_open)
    return seen


def _codes(result):
    return {c for c, _ in severities(result)} - {"ASSET_UNREADABLE"}


def test_file_size_remote_content_range_match(monkeypatch):
    seen = _patch_remote(monkeypatch, _FakeResponse(206, {"Content-Range": "bytes 0-0/1000"}))
    result = audit_item_dict(_remote_item(1000), source="https://example.org/item.json", check_file_size=True)
    assert _codes(result) == set()
    assert seen["range"] == "bytes=0-0"


def test_file_size_remote_content_range_mismatch_is_warn(monkeypatch):
    _patch_remote(monkeypatch, _FakeResponse(206, {"Content-Range": "bytes 0-0/999"}))
    result = audit_item_dict(_remote_item(1000), source="https://example.org/item.json", check_file_size=True)
    assert ("FILE_SIZE_MISMATCH", "WARN") in severities(result)
    assert result.ok


@pytest.mark.parametrize("response", [
    _FakeResponse(206, {"Content-Range": "bytes 0-0/*"}),                       # unknown total
    _FakeResponse(200, {"Content-Length": "5", "Content-Encoding": "gzip"}),    # encoded length
    _FakeResponse(206, {}),                                                      # no usable header
    urllib.error.HTTPError("https://example.org/a.tif", 403, "Forbidden", Message(), io.BytesIO()),
])
def test_file_size_remote_ambiguous_is_skipped(monkeypatch, response):
    _patch_remote(monkeypatch, response)
    result = audit_item_dict(_remote_item(1000), source="https://example.org/item.json", check_file_size=True)
    assert _codes(result) == set()


# --- G4: duplicate hrefs with different declared bands ----------------------

def _dup(tmp_path, first_extra, second_extra):
    item_path, item = make_fixture(tmp_path)
    base = item["assets"]["data"]
    a, b = dict(base), dict(base)
    a.update(first_extra)
    b.update(second_extra)
    item["assets"] = {"a": a, "b": b}
    write_item(item_path, item)
    return audit_item(str(item_path))


def test_duplicate_href_same_bands_stays_generic_warn(tmp_path):
    result = _dup(tmp_path, {}, {})
    assert severities(result) == {("DUPLICATE_DATA_HREF", "WARN")}


def test_duplicate_href_different_polarizations_is_specific_warn(tmp_path):
    result = _dup(tmp_path, {"sar:polarizations": ["VV"]}, {"sar:polarizations": ["VH"]})
    assert severities(result) == {("DUPLICATE_HREF_DIFFERENT_BANDS", "WARN")}
    assert result.ok


def test_duplicate_href_different_band_names_is_specific_warn(tmp_path):
    bands_a = [{"name": "B04", "data_type": "uint16", "nodata": 0}, {"name": "B03", "data_type": "uint16", "nodata": 0}]
    bands_b = [{"name": "B08", "data_type": "uint16", "nodata": 0}, {"name": "B11", "data_type": "uint16", "nodata": 0}]
    result = _dup(tmp_path, {"raster:bands": bands_a}, {"raster:bands": bands_b})
    assert severities(result) == {("DUPLICATE_HREF_DIFFERENT_BANDS", "WARN")}


def test_duplicate_href_different_identifier_kinds_not_escalated(tmp_path):
    # One asset names the band "B04", the other only says common_name "red":
    # not comparable, so no claim of different semantics.
    result = _dup(tmp_path, {"eo:bands": [{"name": "B04"}]}, {"eo:bands": [{"common_name": "red"}]})
    assert "DUPLICATE_HREF_DIFFERENT_BANDS" not in {c for c, _ in severities(result)}
    assert ("DUPLICATE_DATA_HREF", "WARN") in severities(result)
