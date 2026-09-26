"""Offline regression fixtures for behaviour found in live public catalogs.

Each fixture reproduces the *shape* of a live finding with a tiny local
GeoTIFF (or a simulated access failure). No network access is needed.

Wave 2: Microsoft Planetary Computer 3dep-seamless, NASA GHG Center.
Wave 3: blind audit of Digital Earth Australia and DLR terrabyte.
"""
import json

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from stac_integrity.audit import audit_item, audit_item_dict
from stac_integrity.cli import main


def _tif(path, *, crs="EPSG:4326", width=4, height=3, nodata=None, transform=None):
    kwargs = {"nodata": nodata} if nodata is not None else {}
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=1, dtype="float32",
        crs=crs, transform=transform or from_origin(100, 200, 10, 10), **kwargs,
    ) as dst:
        dst.write(np.full((1, height, width), -9999, dtype="float32"))


def _item(asset):
    return {"type": "Feature", "stac_version": "1.1.0", "id": "fixture", "properties": {},
            "geometry": None, "links": [], "assets": {"data": {"type": "image/tiff; application=geotiff",
                                                                "roles": ["data"], **asset}}}


def _write(tmp_path, item):
    p = tmp_path / "item.json"
    p.write_text(json.dumps(item))
    return p


def severities(result):
    return {(f.code, f.severity) for f in result.findings}


# --- FP-1 (Wave 3, DLR terrabyte): remote catalog with file:// hrefs ---------

def _unopenable(*args, **kwargs):
    raise rasterio.errors.RasterioIOError("/dss/internal/example.tif: No such file or directory")


def test_remote_item_with_unreadable_file_href_is_access_warning(monkeypatch):
    monkeypatch.setattr(rasterio, "open", _unopenable)
    item = _item({"href": "file:///dss/internal/example.tif"})
    result = audit_item_dict(item, source="https://example.org/item.json")
    assert severities(result) == {("ASSET_UNREADABLE", "WARN")}
    assert result.ok


def test_remote_item_with_unreadable_file_href_honors_fail_unreadable(monkeypatch):
    monkeypatch.setattr(rasterio, "open", _unopenable)
    item = _item({"href": "file:///dss/internal/example.tif"})
    result = audit_item_dict(item, source="https://example.org/item.json", unreadable_severity="ERROR")
    assert severities(result) == {("ASSET_UNREADABLE", "ERROR")}


def test_remote_item_with_unreadable_file_href_exits_0(monkeypatch, capsys):
    item = _item({"href": "file:///dss/internal/example.tif"})
    monkeypatch.setattr("stac_integrity.audit.load_json", lambda source: item)
    monkeypatch.setattr(rasterio, "open", _unopenable)
    assert main(["item", "https://example.org/item.json"]) == 0
    assert "[WARN] ASSET_UNREADABLE" in capsys.readouterr().out


@pytest.mark.parametrize("href", ["missing.tif", "file:///definitely/not/here.tif"])
def test_local_item_with_missing_local_asset_is_still_error(tmp_path, href):
    result = audit_item(str(_write(tmp_path, _item({"href": href}))))
    assert severities(result) == {("ASSET_UNREADABLE", "ERROR")}
    assert not result.ok


# --- FP-2 (Wave 3, DEA nidem): nodata declared in STAC, absent from header ---

@pytest.mark.parametrize(
    "header_nodata,expected",
    [
        (None, {("NODATA_NOT_IN_HEADER", "WARN")}),   # Case A: header carries no nodata
        (0, {("NODATA_MISMATCH", "ERROR")}),          # Case B: both present, different
        (-9999, set()),                               # Case C: both present, equal
    ],
    ids=["header-absent-warn", "header-differs-error", "header-equal-pass"],
)
def test_declared_nodata_vs_header(tmp_path, header_nodata, expected):
    _tif(tmp_path / "a.tif", nodata=header_nodata)
    item = _item({"href": "a.tif", "raster:bands": [{"data_type": "float32", "nodata": -9999}]})
    result = audit_item(str(_write(tmp_path, item)))
    assert severities(result) == expected
    assert result.ok is (not any(sev == "ERROR" for _, sev in expected))


def test_declared_null_nodata_vs_header_nodata_is_error(tmp_path):
    # STAC explicitly says "no nodata" while the header defines one: the header
    # proves the declaration wrong, so this stays an ERROR.
    _tif(tmp_path / "a.tif", nodata=-9999)
    item = _item({"href": "a.tif", "raster:bands": [{"nodata": None}]})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {("NODATA_MISMATCH", "ERROR")}


# --- Wave 3 TRUE_MISMATCH shapes (must keep being ERRORs) --------------------

def test_dea_nidem_like_crs_declared_geographic_but_asset_is_albers(tmp_path):
    t = from_origin(-1785175, -3178425, 25, 25)
    _tif(tmp_path / "a.tif", crs="EPSG:3577", width=5, height=4, transform=t, nodata=-9999)
    item = _item({"href": "a.tif", "proj:code": "EPSG:4326", "proj:shape": [4, 5],
                  "proj:transform": [25, 0, -1785175, 0, -25, -3178425],
                  "raster:bands": [{"data_type": "float32", "nodata": -9999}]})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {("CRS_MISMATCH", "ERROR")}


def test_dea_mstp_like_shape_off_by_one(tmp_path):
    _tif(tmp_path / "a.tif", width=5, height=5, transform=from_origin(134, -23, 0.2, 0.2))
    item = _item({"href": "a.tif", "proj:code": "EPSG:4326", "proj:shape": [4, 4],
                  "proj:transform": [0.2, 0, 134, 0, -0.2, -23]})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {("SHAPE_MISMATCH", "ERROR")}


# --- Wave 2 regressions ------------------------------------------------------

def test_pc_3dep_like_compound_crs_is_vertical_warning(tmp_path):
    _tif(tmp_path / "a.tif", crs="EPSG:4269")
    item = _item({"href": "a.tif", "proj:epsg": 5498})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {("CRS_VERTICAL_UNVERIFIED", "WARN")}


def test_pc_3dep_like_pixel_size_error_is_still_detected(tmp_path):
    _tif(tmp_path / "a.tif", crs="EPSG:4269", transform=from_origin(-105.0005556, 31.0005556, 9.259259e-05, 9.259259e-05))
    item = _item({"href": "a.tif", "proj:epsg": 5498, "proj:shape": [3, 4],
                  "proj:transform": [1e-05, 0, -105.0005556, 0, -1e-05, 31.0005556]})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {
        ("CRS_VERTICAL_UNVERIFIED", "WARN"), ("TRANSFORM_MISMATCH", "ERROR")}


def test_ghg_center_like_float_epsg(tmp_path):
    _tif(tmp_path / "a.tif", crs="EPSG:4326")
    item = _item({"href": "a.tif", "proj:epsg": 4326.0})
    assert audit_item(str(_write(tmp_path, item))).findings == []


# --- FP-3 (NRP public-data plant-richness, found by the Track F sweep) -------

@pytest.mark.parametrize("declared", [-3.4e38, "-3.4e+38", -3.3999999521443642e38])
def test_float32_nodata_compared_at_band_precision(tmp_path, declared):
    # GDAL stores float32 nodata, so STAC -3.4e38 reads back as
    # -3.3999999521e38. The same float32 value must not be a NODATA_MISMATCH.
    _tif(tmp_path / "a.tif", nodata=-3.4e38)
    item = _item({"href": "a.tif", "raster:bands": [{"data_type": "float32", "nodata": declared}]})
    assert audit_item(str(_write(tmp_path, item))).findings == []


def test_float32_nodata_that_really_differs_is_still_error(tmp_path):
    _tif(tmp_path / "a.tif", nodata=-3.4e38)
    item = _item({"href": "a.tif", "raster:bands": [{"data_type": "float32", "nodata": -9999}]})
    assert severities(audit_item(str(_write(tmp_path, item)))) == {("NODATA_MISMATCH", "ERROR")}
