import json
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin

from stac_integrity.collection import audit_collection


def _make_tif(path: Path, *, count=1):
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        width=4,
        height=3,
        count=count,
        dtype="uint16",
        crs="EPSG:32632",
        transform=from_origin(100, 200, 10, 10),
        nodata=0,
    ) as dst:
        dst.write(np.ones((count, 3, 4), dtype="uint16"))


def _item(item_id: str, href: str):
    return {
        "type": "Feature",
        "stac_version": "1.0.0",
        "id": item_id,
        "bbox": [0, 0, 1, 1],
        "geometry": None,
        "properties": {},
        "assets": {"data": {"href": href, "roles": ["data"]}},
        "links": [],
    }


def test_static_collection_audits_items_without_inheriting_item_assets(tmp_path):
    _make_tif(tmp_path / "a.tif")
    _make_tif(tmp_path / "b.tif")
    a = _item("a", "a.tif")
    b = _item("b", "b.tif")
    for item in (a, b):
        item["assets"]["data"].update({
            "type": "image/tiff; application=geotiff",
            "proj:code": "EPSG:32632",
            "proj:shape": [3, 4],
            "proj:transform": [10, 0, 100, 0, -10, 200],
            "proj:bbox": [100, 170, 140, 200],
            "bands": [{"data_type": "uint16", "nodata": 0}],
        })
    (tmp_path / "a.json").write_text(json.dumps(a))
    (tmp_path / "b.json").write_text(json.dumps(b))

    collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": "c",
        "description": "fixture",
        "license": "proprietary",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}, "temporal": {"interval": [[None, None]]}},
        "item_assets": {
            "data": {
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "proj:shape": [999, 999]
            }
        },
        "links": [
            {"rel": "item", "href": "a.json"},
            {"rel": "item", "href": "b.json"},
        ],
    }
    cp = tmp_path / "collection.json"
    cp.write_text(json.dumps(collection))

    result = audit_collection(str(cp), workers=2)
    assert result.ok
    assert result.items_checked == 2
    assert result.checked_assets == 2
    assert result.error_count == 0


def test_collection_aggregates_known_bad_band_count(tmp_path):
    _make_tif(tmp_path / "bad.tif", count=6)
    bad = _item("bad", "bad.tif")
    bad["assets"]["data"].update({
        "type": "image/tiff; application=geotiff",
        "bands": [{"data_type": "uint16", "nodata": 0}],
    })
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    collection = {
        "type": "Collection",
        "stac_version": "1.0.0",
        "id": "bad-c",
        "description": "fixture",
        "license": "proprietary",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}, "temporal": {"interval": [[None, None]]}},
        "item_assets": {
            "data": {
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "bands": [{"data_type": "uint16", "nodata": 0}],
            }
        },
        "links": [{"rel": "item", "href": "bad.json"}],
    }
    cp = tmp_path / "collection.json"
    cp.write_text(json.dumps(collection))

    result = audit_collection(str(cp), workers=1)
    assert not result.ok
    assert result.failing_items == 1
    assert result.codes["BAND_COUNT_MISMATCH"] == 1


def test_collection_level_asset_band_count_mismatch(tmp_path):
    _make_tif(tmp_path / "collection-data.tif", count=6)
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": "collection-assets-bad",
        "description": "fixture",
        "license": "proprietary",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}, "temporal": {"interval": [[None, None]]}},
        "assets": {
            "data": {
                "href": "collection-data.tif",
                "type": "image/tiff; application=geotiff",
                "roles": ["data"],
                "bands": [{"name": "pfg", "data_type": "uint16", "nodata": 0}],
            }
        },
        "links": [],
    }
    cp = tmp_path / "collection.json"
    cp.write_text(json.dumps(collection))

    result = audit_collection(str(cp), workers=1)
    assert not result.ok
    assert result.collection_assets_failed
    assert result.items_checked == 0
    assert result.codes["BAND_COUNT_MISMATCH"] == 1
