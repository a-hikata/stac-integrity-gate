"""Zarr semantic checks (Track E).

The pure comparison tests and the "extra missing" test run in the base suite.
Tests that build real Zarr stores call ``pytest.importorskip("zarr")``.
Fixtures reproduce the metadata shapes observed live on
stac.core.eopf.eodc.eu (EOPF Sample Service #82, EOPF Explorer data-model #195).
"""
import json
from pathlib import Path

import pytest

from stac_integrity import zarr_checks
from stac_integrity.audit import audit_item
from stac_integrity.zarr_checks import ArrayFacts, compare_array, is_zarr_asset

EOPF_CF = {"scale_factor": 0.0001, "add_offset": -0.1, "units": "digital_counts"}


def codes(findings):
    return [f.code for f in findings]


def facts(**kw):
    base = dict(path="b04", shape=(3, 4), dtype="uint16", fill_value=0, attrs=dict(EOPF_CF), dims=("y", "x"))
    base.update(kw)
    return ArrayFacts(**base)


# ------------------------------------------------------------ pure (no zarr)


@pytest.mark.parametrize(
    "asset,href,expected",
    [
        ({"type": "application/vnd+zarr"}, "https://x/p.zarr/m/b04", True),
        ({"type": "application/vnd.zarr; version=3"}, "https://x/p/r10m", True),
        ({}, "https://x/store.zarr", True),
        ({}, "/data/store.zarr/", True),
        ({"type": "image/tiff; application=geotiff"}, "https://x/a.tif", False),
        ({}, "https://x/zarrlike.tif", False),
    ],
)
def test_is_zarr_asset(asset, href, expected):
    assert is_zarr_asset(asset, href) is expected


def test_double_scaling_risk_is_warn_when_values_repeat():
    # EOPF sentinel-2-l2a B04_10m: asset raster:scale/offset == array CF attrs.
    asset = {"data_type": "uint16", "nodata": 0, "raster:scale": 0.0001, "raster:offset": -0.1}
    out = compare_array({}, asset, "B04_10m", facts(), None, "bands[0]")
    assert codes(out) == ["ZARR_DOUBLE_SCALING_RISK"]
    assert out[0].severity == "WARN"
    assert out[0].field == "raster:scale"
    assert out[0].actual == {"scale_factor": 0.0001, "add_offset": -0.1}


def test_band_level_scale_is_preferred_over_asset_level():
    band = {"name": "b04", "scale": 0.0001, "offset": -0.1}
    out = compare_array({}, {}, "SR", facts(), band, "raster:bands[0]")
    assert codes(out) == ["ZARR_DOUBLE_SCALING_RISK"]
    assert out[0].field == "raster:bands[0].scale"


def test_differing_scaling_is_conflict_warn_not_error():
    asset = {"raster:scale": 0.001, "raster:offset": 0}
    out = compare_array({}, asset, "a", facts(), None, "bands[0]")
    assert codes(out) == ["ZARR_SCALE_CONFLICT"]
    assert out[0].severity == "WARN"


@pytest.mark.parametrize(
    "asset,attrs",
    [
        # SCL_20m: identity scaling on both sides is a no-op.
        ({"raster:scale": 1, "raster:offset": 0}, {"scale_factor": 1, "add_offset": 0}),
        # Fix proposed in #82: STAC drops scale, store keeps CF attrs.
        ({}, EOPF_CF),
        # COG-like pattern: only STAC describes scaling.
        ({"raster:scale": 0.0001, "raster:offset": -0.1}, {}),
        # Identity STAC + active CF.
        ({"raster:scale": 1.0}, EOPF_CF),
    ],
)
def test_no_scaling_finding_without_two_active_scalings(asset, attrs):
    assert compare_array({}, asset, "a", facts(attrs=attrs), None, "bands[0]") == []


def test_dtype_mismatch_is_error():
    out = compare_array({}, {"data_type": "uint8"}, "a", facts(attrs={}), None, "bands[0]")
    assert codes(out) == ["DATA_TYPE_MISMATCH"]
    assert out[0].severity == "ERROR"


def test_float_dtype_over_cf_encoded_int_is_decoded_warn():
    out = compare_array({}, {"data_type": "float32"}, "a", facts(), None, "bands[0]")
    assert codes(out) == ["ZARR_DATA_TYPE_DECODED"]
    assert out[0].severity == "WARN"


def test_nodata_rules():
    # CF _FillValue is semantic: mismatch is an ERROR.
    out = compare_array({}, {"nodata": 0}, "a", facts(attrs={"_FillValue": 65535}), None, "b")
    assert [(f.severity, f.code) for f in out] == [("ERROR", "NODATA_MISMATCH")]
    # Only the storage fill_value: WARN.
    out = compare_array({}, {"nodata": 65535}, "a", facts(attrs={}), None, "b")
    assert [(f.severity, f.code) for f in out] == [("WARN", "ZARR_FILL_VALUE_DIFFERS")]
    # Nothing in the store.
    out = compare_array({}, {"nodata": 0}, "a", facts(attrs={}, fill_value=None), None, "b")
    assert codes(out) == ["ZARR_NODATA_NOT_IN_STORE"]
    # NaN strings normalise.
    out = compare_array({}, {"nodata": "nan"}, "a", facts(dtype="float32", attrs={}, fill_value=float("nan")), None, "b")
    assert out == []


def test_shape_rules():
    out = compare_array({}, {"proj:shape": [3, 5]}, "a", facts(), None, "b")
    assert [(f.severity, f.code) for f in out] == [("ERROR", "SHAPE_MISMATCH")]
    # (time, y, x) cube: last two dims are spatial by name.
    cube = facts(shape=(7, 3, 4), dims=("time", "y", "x"))
    assert compare_array({}, {"proj:shape": [3, 4]}, "a", cube, None, "b") == []
    # Unnamed N-D layout: cannot tell which dims are rows/cols.
    odd = facts(shape=(3, 4, 2), dims=None)
    assert codes(compare_array({}, {"proj:shape": [3, 4]}, "a", odd, None, "b")) == ["ZARR_SHAPE_UNVERIFIED"]


def test_missing_extra_skips_with_warn(tmp_path, monkeypatch):
    monkeypatch.setattr(zarr_checks, "zarr_module", lambda: None)
    item = {
        "type": "Feature", "stac_version": "1.1.0", "id": "z", "properties": {}, "links": [],
        "assets": {"B04_10m": {"href": "p.zarr/b04", "type": "application/vnd+zarr", "roles": ["data"]}},
    }
    path = tmp_path / "item.json"
    path.write_text(json.dumps(item))
    result = audit_item(str(path))
    assert result.ok
    assert result.checked_assets == 0 and result.skipped_assets == 1
    assert codes(result.findings) == ["ZARR_SUPPORT_UNAVAILABLE", "NO_RASTER_ASSETS"]
    assert all(f.severity == "WARN" for f in result.findings)


# --------------------------------------------------------- real Zarr stores


def _zarr():
    return pytest.importorskip("zarr")


def _formats():
    zarr = pytest.importorskip("zarr")
    return [2, 3] if int(zarr.__version__.split(".")[0]) >= 3 else [2]


def write_array(group, name, data, fill_value, attrs, fmt):
    zarr = _zarr()
    if int(zarr.__version__.split(".")[0]) >= 3:
        kw = {"dimension_names": ["y", "x"]} if fmt == 3 else {}
        arr = group.create_array(name, shape=data.shape, dtype=data.dtype, fill_value=fill_value, **kw)
    else:
        arr = group.create_dataset(name, shape=data.shape, dtype=data.dtype, fill_value=fill_value)
    arr[...] = data
    if fmt == 2:
        attrs = {"_ARRAY_DIMENSIONS": ["y", "x"], **attrs}
    arr.attrs.update(attrs)
    return arr


def make_store(tmp_path: Path, fmt: int) -> Path:
    """EOPF-like product: measurements/reflectance/r10m/{b02,b04,x,y}."""
    import numpy as np

    zarr = _zarr()
    path = tmp_path / "S2_MSIL2A.zarr"
    kw = {"zarr_format": fmt} if int(zarr.__version__.split(".")[0]) >= 3 else {}
    root = zarr.open_group(str(path), mode="w", **kw)
    r10m = root.require_group("measurements").require_group("reflectance").require_group("r10m")
    data = np.arange(12, dtype="uint16").reshape(3, 4) + 141
    for band in ("b02", "b04"):
        write_array(r10m, band, data, 0, dict(EOPF_CF), fmt)
    orbit = root.require_group("ascending")
    for var in ("vv", "vh", "border_mask"):
        write_array(orbit, var, data.astype("float32"), 0, {}, fmt)
    return path


def write_item(tmp_path, assets):
    item = {"type": "Feature", "stac_version": "1.1.0", "id": "fixture", "properties": {}, "links": [],
            "assets": assets}
    path = tmp_path / "item.json"
    path.write_text(json.dumps(item))
    return path


@pytest.fixture(params=[2, 3])
def fmt(request):
    if request.param not in _formats():
        pytest.skip("zarr v3 format needs zarr-python >= 3")
    return request.param


def test_eopf_per_band_array_double_scaling(tmp_path, fmt):
    """sentinel-2-l2a (CPM 2.7.0) shape: one asset per array + raster:scale/offset."""
    make_store(tmp_path, fmt)
    path = write_item(tmp_path, {
        "B04_10m": {"href": "S2_MSIL2A.zarr/measurements/reflectance/r10m/b04", "type": "application/vnd+zarr",
                    "roles": ["data"], "data_type": "uint16", "nodata": 0, "proj:shape": [3, 4],
                    "raster:scale": 0.0001, "raster:offset": -0.1,
                    "bands": [{"name": "B04", "eo:common_name": "red"}]},
    })
    result = audit_item(str(path))
    assert result.checked_assets == 1
    assert result.ok
    assert codes(result.findings) == ["ZARR_DOUBLE_SCALING_RISK"]


def test_eopf_fixed_group_asset_is_clean(tmp_path, fmt):
    """sentinel-2-l2a-zarr3 shape after the #82 fix: group href + bands, no raster:scale."""
    make_store(tmp_path, fmt)
    path = write_item(tmp_path, {
        "SR_10m": {"href": "S2_MSIL2A.zarr/measurements/reflectance/r10m", "type": "application/vnd.zarr; version=3",
                   "roles": ["data", "reflectance"], "data_type": "uint16", "nodata": 0, "proj:shape": [3, 4],
                   "bands": [{"name": "b02"}, {"name": "b04"}]},
        "product": {"href": "S2_MSIL2A.zarr", "type": "application/vnd.zarr; version=3", "roles": ["metadata"]},
    })
    result = audit_item(str(path))
    assert result.checked_assets == 1 and result.skipped_assets == 1
    assert result.findings == []


def test_group_asset_contradictions_are_errors(tmp_path, fmt):
    make_store(tmp_path, fmt)
    path = write_item(tmp_path, {
        "SR_10m": {"href": "S2_MSIL2A.zarr/measurements/reflectance/r10m", "type": "application/vnd+zarr",
                   "roles": ["data"], "data_type": "uint8", "proj:shape": [30, 40],
                   "bands": [{"name": "b04"}, {"name": "b99"}]},
    })
    result = audit_item(str(path))
    assert not result.ok
    assert sorted(codes(result.findings)) == ["DATA_TYPE_MISMATCH", "SHAPE_MISMATCH", "ZARR_BAND_UNRESOLVED"]


def test_orbit_group_without_band_names_is_unresolved_warn(tmp_path, fmt):
    """EOPF Explorer S1 RTC (#195): two data assets on the same orbit group."""
    make_store(tmp_path, fmt)
    href = "S2_MSIL2A.zarr/ascending"
    path = write_item(tmp_path, {
        # Declares band fields that cannot be tied to one array of the group.
        "gamma0-rtc-backscatter-asc": {"href": href, "type": "application/vnd.zarr; version=3", "roles": ["data"],
                                       "data_type": "float32", "nodata": 0},
        # Declares nothing comparable: no Zarr finding.
        "border-mask-asc": {"href": href, "type": "application/vnd.zarr; version=3", "roles": ["data"]},
    })
    result = audit_item(str(path))
    assert result.ok
    assert sorted(codes(result.findings)) == ["DUPLICATE_DATA_HREF", "ZARR_GROUP_UNRESOLVED"]


def test_group_bands_resolve_case_insensitively(tmp_path, fmt):
    """sentinel-2-l2a SR_10m declares B02/B04 while the arrays are b02/b04."""
    make_store(tmp_path, fmt)
    path = write_item(tmp_path, {
        "SR_10m": {"href": "S2_MSIL2A.zarr/measurements/reflectance/r10m", "type": "application/vnd+zarr",
                   "roles": ["data"], "raster:scale": 0.0001, "raster:offset": -0.1,
                   "bands": [{"name": "B02"}, {"name": "B04"}]},
    })
    result = audit_item(str(path))
    assert codes(result.findings) == ["ZARR_DOUBLE_SCALING_RISK", "ZARR_DOUBLE_SCALING_RISK"]


def test_missing_local_store_is_error(tmp_path):
    _zarr()
    path = write_item(tmp_path, {"d": {"href": "missing.zarr", "type": "application/vnd+zarr", "roles": ["data"]}})
    result = audit_item(str(path))
    assert [(f.severity, f.code) for f in result.findings] == [("ERROR", "ASSET_UNREADABLE")]
