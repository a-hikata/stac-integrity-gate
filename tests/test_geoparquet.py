"""GeoParquet / Table extension checks. Fixtures are tiny Parquet files written
into tmp_path; everything is offline."""

import json

import pytest

from stac_integrity import geoparquet_checks
from stac_integrity.audit import audit_item_dict

PARQUET = "application/vnd.apache.parquet"



def _item(tmp_path, asset_extra=None, properties=None, name="data.parquet"):
    asset = {"href": name, "type": PARQUET, "roles": ["data"]}
    asset.update(asset_extra or {})
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "id": "t",
        "geometry": None,
        "properties": properties or {},
        "assets": {"table": asset},
        "links": [],
    }


def _audit(tmp_path, item):
    return audit_item_dict(item, source=str(tmp_path / "item.json"))


def codes(result):
    return [f.code for f in result.findings]


def by_code(result, code):
    return [f for f in result.findings if f.code == code]


# ---------------------------------------------------------------- no pyarrow


def test_parquet_asset_without_pyarrow_is_warn_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(geoparquet_checks, "pyarrow_available", lambda: False)
    item = _item(tmp_path, {"table:row_count": 999})
    result = _audit(tmp_path, item)
    assert result.ok
    assert "GEOPARQUET_DEPENDENCY_MISSING" in codes(result)
    assert result.checked_assets == 0 and result.skipped_assets == 1


def test_is_parquet_asset_detection():
    assert geoparquet_checks.is_parquet_asset({"type": "application/x-parquet"}, "x")
    assert geoparquet_checks.is_parquet_asset({}, "https://h/a/b.parquet")
    assert not geoparquet_checks.is_parquet_asset({"type": "image/tiff"}, "a.tif")


def test_type_families():
    f = geoparquet_checks._type_family
    assert f("int32") == f("int16") == f("integer") == "integer"
    assert f("double") == f("float64") == f("float") == "float"
    assert f("large_string") == f("string") == "string"
    assert f("timestamp[us, tz=UTC]") == f("datetime") == "timestamp"
    assert f("geometry") is None


# ---------------------------------------------------------------- with pyarrow


@pytest.fixture
def pa():
    return pytest.importorskip("pyarrow")


def _write(tmp_path, pa, *, name="data.parquet", geo="default", columns=None, rows=3):
    import pyarrow.parquet as pq

    columns = columns or {
        "id": pa.array(list(range(rows)), pa.int32()),
        "label": pa.array([f"r{i}" for i in range(rows)], pa.string()),
        "geometry": pa.array([b"\x01\x01\x00\x00\x00" + b"\x00" * 16] * rows, pa.binary()),
    }
    table = pa.table(columns)
    if geo == "default":
        geo = {
            "version": "1.1.0",
            "primary_column": "geometry",
            "columns": {"geometry": {"encoding": "WKB", "geometry_types": ["Point"]}},
        }
    if geo is not None:
        md = dict(table.schema.metadata or {})
        md[b"geo"] = geo if isinstance(geo, bytes) else json.dumps(geo).encode()
        table = table.replace_schema_metadata(md)
    pq.write_table(table, tmp_path / name)
    return tmp_path / name


GOOD_COLUMNS = [
    {"name": "id", "type": "int32"},
    {"name": "label", "type": "string"},
    {"name": "geometry", "type": "geometry"},
]


def test_known_good_geoparquet(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, {
        "table:columns": GOOD_COLUMNS,
        "table:row_count": 3,
        "table:primary_geometry": "geometry",
        "proj:code": "EPSG:4326",  # geo crs absent => OGC:CRS84, equivalent in GeoParquet
    })
    result = _audit(tmp_path, item)
    assert result.findings == []
    assert result.checked_assets == 1


def test_missing_declared_column_is_error(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:columns": GOOD_COLUMNS + [{"name": "area_km2", "type": "double"}]})
    result = _audit(tmp_path, item)
    [f] = by_code(result, "TABLE_COLUMN_MISSING")
    assert f.severity == "ERROR" and f.declared == "area_km2"


def test_missing_column_inherited_from_properties_is_warn(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, properties={"table:columns": [{"name": "area_km2"}]})
    [f] = by_code(_audit(tmp_path, item), "TABLE_COLUMN_MISSING")
    assert f.severity == "WARN"


def test_case_only_column_difference_is_warn(tmp_path, pa):
    # Real-world pattern (NRP public-high-seas vme.parquet: 'surface' vs 'SURFACE').
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:columns": [{"name": "LABEL"}]})
    result = _audit(tmp_path, item)
    assert result.ok
    [f] = by_code(result, "TABLE_COLUMN_CASE_MISMATCH")
    assert f.severity == "WARN" and f.actual == "label"


def test_partition_key_column_is_warn(tmp_path, pa):
    (tmp_path / "h0=5").mkdir()
    _write(tmp_path, pa, name="h0=5/data_0.parquet")
    item = _item(tmp_path, {"table:columns": [{"name": "h0"}, {"name": "id"}]}, name="h0=5/data_0.parquet")
    result = _audit(tmp_path, item)
    assert result.ok
    assert by_code(result, "TABLE_COLUMN_PARTITION_KEY")[0].severity == "WARN"


def test_extra_undeclared_columns_are_silent(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:columns": [{"name": "id"}]})
    assert _audit(tmp_path, item).findings == []


def test_wrong_row_count_is_error(tmp_path, pa):
    # Real-world pattern (NRP public-wyoming blm-sma.parquet: 439200 vs 865059).
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:row_count": 5})
    [f] = by_code(_audit(tmp_path, item), "TABLE_ROW_COUNT_MISMATCH")
    assert f.severity == "ERROR" and f.declared == 5 and f.actual == 3


def test_row_count_inherited_is_warn(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, properties={"table:row_count": 5})
    [f] = by_code(_audit(tmp_path, item), "TABLE_ROW_COUNT_MISMATCH")
    assert f.severity == "WARN"


def test_row_count_float_integer_ok_and_invalid_warn(tmp_path, pa):
    _write(tmp_path, pa)
    assert _audit(tmp_path, _item(tmp_path, {"table:row_count": 3.0})).findings == []
    r = _audit(tmp_path, _item(tmp_path, {"table:row_count": "3"}))
    assert codes(r) == ["TABLE_ROW_COUNT_INVALID"] and r.ok


def test_missing_primary_geometry_column_is_error(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:primary_geometry": "geom"})
    [f] = by_code(_audit(tmp_path, item), "PRIMARY_GEOMETRY_MISSING")
    assert f.severity == "ERROR"


def test_primary_geometry_not_in_geo_metadata_is_warn(tmp_path, pa):
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:primary_geometry": "label"})
    result = _audit(tmp_path, item)
    assert result.ok
    assert "PRIMARY_GEOMETRY_NOT_GEOMETRY" in codes(result)


def test_primary_geometry_differs_from_geo_primary_is_warn(tmp_path, pa):
    rows = 2
    wkb = pa.array([b"\x01\x01\x00\x00\x00" + b"\x00" * 16] * rows, pa.binary())
    geo = {
        "version": "1.1.0",
        "primary_column": "geometry",
        "columns": {"geometry": {"encoding": "WKB", "geometry_types": []},
                    "centroid": {"encoding": "WKB", "geometry_types": []}},
    }
    _write(tmp_path, pa, geo=geo, columns={"geometry": wkb, "centroid": wkb})
    result = _audit(tmp_path, _item(tmp_path, {"table:primary_geometry": "centroid"}))
    assert result.ok
    assert "PRIMARY_GEOMETRY_DIFFERS" in codes(result)


def test_missing_geo_metadata_is_warn(tmp_path, pa):
    _write(tmp_path, pa, geo=None)
    result = _audit(tmp_path, _item(tmp_path, {"table:primary_geometry": "geometry"}))
    assert result.ok
    assert codes(result) == ["GEOPARQUET_METADATA_MISSING"]


def test_plain_parquet_without_geo_claims_is_silent(tmp_path, pa):
    _write(tmp_path, pa, geo=None)
    assert _audit(tmp_path, _item(tmp_path, {"table:row_count": 3})).findings == []


def test_invalid_geo_metadata_is_warn(tmp_path, pa):
    _write(tmp_path, pa, geo=b"{not json")
    result = _audit(tmp_path, _item(tmp_path, {"table:primary_geometry": "geometry"}))
    assert result.ok
    assert "GEOPARQUET_METADATA_INVALID" in codes(result)


def _geo_with_crs(crs):
    col = {"encoding": "WKB", "geometry_types": ["Point"]}
    if crs != "absent":
        col["crs"] = crs
    return {"version": "1.1.0", "primary_column": "geometry", "columns": {"geometry": col}}


def test_crs_mismatch_is_error_when_both_sides_are_epsg(tmp_path, pa):
    from rasterio.crs import CRS

    projjson = CRS.from_epsg(4326).to_dict(projjson=True)
    _write(tmp_path, pa, geo=_geo_with_crs(projjson))
    [f] = by_code(_audit(tmp_path, _item(tmp_path, {"proj:code": "EPSG:32632"})), "CRS_MISMATCH")
    assert f.severity == "ERROR"


def test_crs_mismatch_inherited_is_warn(tmp_path, pa):
    _write(tmp_path, pa, geo=_geo_with_crs("absent"))
    result = _audit(tmp_path, _item(tmp_path, properties={"proj:code": "EPSG:32632"}))
    assert result.ok
    assert "CRS_MISMATCH_UNCERTAIN" in codes(result)


def test_epsg4326_vs_crs84_default_is_equivalent(tmp_path, pa):
    # Real-world pattern (NRP public-wetlands nwi-v2.parquet: proj:epsg 4326, geo crs absent).
    _write(tmp_path, pa, geo=_geo_with_crs("absent"))
    assert _audit(tmp_path, _item(tmp_path, {"proj:epsg": 4326})).findings == []


def test_projjson_same_crs_is_equivalent(tmp_path, pa):
    from rasterio.crs import CRS

    projjson = CRS.from_epsg(32632).to_dict(projjson=True)
    _write(tmp_path, pa, geo=_geo_with_crs(projjson))
    assert _audit(tmp_path, _item(tmp_path, {"proj:code": "EPSG:32632"})).findings == []


def test_null_geo_crs_is_warn(tmp_path, pa):
    _write(tmp_path, pa, geo=_geo_with_crs(None))
    result = _audit(tmp_path, _item(tmp_path, {"proj:code": "EPSG:4326"}))
    assert result.ok
    assert codes(result) == ["GEOPARQUET_CRS_UNDEFINED"]


def test_type_ambiguity_is_warn_only(tmp_path, pa):
    _write(tmp_path, pa)
    cols = [
        {"name": "id", "type": "double"},        # int32 actual: family mismatch -> WARN
        {"name": "label", "type": "int32"},       # string actual -> WARN
        {"name": "geometry", "type": "geometry"},  # geometry column: never compared
    ]
    result = _audit(tmp_path, _item(tmp_path, {"table:columns": cols}))
    assert result.ok
    found = by_code(result, "TABLE_COLUMN_TYPE_MISMATCH")
    assert {f.declared for f in found} == {"double", "int32"}
    assert all(f.severity == "WARN" for f in found)


def test_compatible_type_vocabularies_are_silent(tmp_path, pa):
    _write(tmp_path, pa)
    cols = [{"name": "id", "type": "number"}, {"name": "label", "type": "large_string"},
            {"name": "geometry", "type": "binary"}]
    assert _audit(tmp_path, _item(tmp_path, {"table:columns": cols})).findings == []


def test_glob_href_is_warn_and_skipped(tmp_path, pa):
    item = _item(tmp_path, {"table:row_count": 1}, name="hex/h0=*/data_0.parquet")
    result = _audit(tmp_path, item)
    assert result.ok
    assert "PARQUET_PARTITIONED_UNVERIFIED" in codes(result)
    assert result.checked_assets == 0


def test_missing_local_parquet_in_local_catalog_is_error(tmp_path, pa):
    result = _audit(tmp_path, _item(tmp_path, name="nope.parquet"))
    [f] = by_code(result, "ASSET_UNREADABLE")
    assert f.severity == "ERROR"


def test_corrupt_parquet_unreadable(tmp_path, pa):
    (tmp_path / "bad.parquet").write_bytes(b"not parquet")
    result = _audit(tmp_path, _item(tmp_path, name="bad.parquet"))
    assert "ASSET_UNREADABLE" in codes(result)


def test_http_range_reader_reads_footer(tmp_path, pa):
    """Serve a fixture over a local HTTP server that supports Range requests."""
    import http.server
    import threading
    from functools import partial

    _write(tmp_path, pa)

    class RangeHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.translate_path(self.path)
            data = open(path, "rb").read()
            rng = self.headers.get("Range", "")
            start, end = 0, len(data) - 1
            if rng.startswith("bytes=-"):
                start = max(0, len(data) - int(rng[7:]))
            elif rng.startswith("bytes="):
                a, b = rng[6:].split("-")
                start, end = int(a), int(b)
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(tmp_path)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/data.parquet"
        item = _item(tmp_path, {"table:row_count": 4}, name=url)
        result = audit_item_dict(item, source=f"http://127.0.0.1:{server.server_address[1]}/item.json")
        [f] = by_code(result, "TABLE_ROW_COUNT_MISMATCH")
        assert f.severity == "ERROR" and f.actual == 3
    finally:
        server.shutdown()


def test_raster_behaviour_unchanged_for_mixed_item(tmp_path, pa):
    """A parquet asset next to a non-data thumbnail: raster path untouched."""
    _write(tmp_path, pa)
    item = _item(tmp_path, {"table:row_count": 3})
    item["assets"]["thumb"] = {"href": "thumb.png", "type": "image/png", "roles": ["thumbnail"]}
    result = _audit(tmp_path, item)
    assert result.findings == [] and result.checked_assets == 1 and result.skipped_assets == 1
