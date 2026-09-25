import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from stac_integrity.audit import audit_item


def make_fixture(tmp_path: Path):
    tif = tmp_path / "asset.tif"
    transform = from_origin(100, 200, 10, 10)
    with rasterio.open(
        tif,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=2,
        dtype="uint16",
        crs="EPSG:32632",
        transform=transform,
        nodata=0,
    ) as dst:
        dst.write(np.ones((2, 3, 4), dtype="uint16"))

    item = {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": "fixture",
        "bbox": [0, 0, 1, 1],
        "geometry": None,
        "properties": {},
        "assets": {
            "data": {
                "href": "asset.tif",
                "type": "image/tiff; application=geotiff; profile=cloud-optimized",
                "roles": ["data"],
                "proj:code": "EPSG:32632",
                "proj:shape": [3, 4],
                "proj:transform": [10, 0, 100, 0, -10, 200],
                "proj:bbox": [100, 170, 140, 200],
                "raster:bands": [
                    {"data_type": "uint16", "nodata": 0},
                    {"data_type": "uint16", "nodata": 0},
                ],
            }
        },
        "links": [],
    }
    item_path = tmp_path / "item.json"
    item_path.write_text(json.dumps(item))
    return item_path, item


def codes(result):
    return [f.code for f in result.findings]


def write_item(path: Path, item):
    path.write_text(json.dumps(item))


def test_good_item_passes(tmp_path):
    item_path, _ = make_fixture(tmp_path)
    result = audit_item(str(item_path))
    assert result.ok
    assert result.findings == []


def test_shape_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["proj:shape"] = [4, 3]
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "SHAPE_MISMATCH" in codes(result)


def test_transform_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["proj:transform"][2] += 20
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "TRANSFORM_MISMATCH" in codes(result)


def test_bbox_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["proj:bbox"][0] += 10
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "BBOX_MISMATCH" in codes(result)


def test_crs_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["proj:code"] = "EPSG:4326"
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "CRS_MISMATCH" in codes(result)


def test_band_count_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["raster:bands"] = [{"data_type": "uint16", "nodata": 0}]
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "BAND_COUNT_MISMATCH" in codes(result)


def test_dtype_and_nodata_mismatch(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["raster:bands"][0] = {"data_type": "float32", "nodata": -9999}
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "DATA_TYPE_MISMATCH" in codes(result)
    assert "NODATA_MISMATCH" in codes(result)


def test_item_level_projection_fallback(tmp_path):
    item_path, item = make_fixture(tmp_path)
    asset = item["assets"]["data"]
    item["properties"]["proj:code"] = asset.pop("proj:code")
    item["properties"]["proj:shape"] = asset.pop("proj:shape")
    item["properties"]["proj:transform"] = asset.pop("proj:transform")
    item["properties"]["proj:bbox"] = asset.pop("proj:bbox")
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert result.ok


def test_visual_asset_skipped_by_default(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["roles"] = ["visual"]
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert result.checked_assets == 0
    assert result.skipped_assets == 1
    assert "NO_RASTER_ASSETS" in codes(result)


def test_visual_asset_can_be_opted_in(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["roles"] = ["visual"]
    write_item(item_path, item)
    result = audit_item(str(item_path), data_assets_only=False)
    assert result.checked_assets == 1
    assert result.ok


def test_stac_11_bands_supported(tmp_path):
    item_path, item = make_fixture(tmp_path)
    asset = item["assets"]["data"]
    asset["bands"] = asset.pop("raster:bands")
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert result.ok
    assert result.findings == []


def test_invalid_nine_element_transform_tail(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["proj:transform"] = [10, 0, 100, 0, -10, 200, 0, 1, 1]
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "TRANSFORM_INVALID" in codes(result)


def test_duplicate_data_href_warns(tmp_path):
    item_path, item = make_fixture(tmp_path)
    first = item["assets"]["data"]
    item["assets"]["other"] = dict(first)
    write_item(item_path, item)
    result = audit_item(str(item_path))
    assert "DUPLICATE_DATA_HREF" in codes(result)
    assert result.ok
