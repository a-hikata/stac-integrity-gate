"""Create a 6-band GeoTIFF whose STAC metadata falsely declares one band."""
import json
from pathlib import Path
import numpy as np
import rasterio
from rasterio.transform import from_origin

out = Path("known_bad")
out.mkdir(exist_ok=True)
tif = out / "six_band.tif"
with rasterio.open(
    tif, "w", driver="GTiff", width=8, height=8, count=6, dtype="uint8",
    crs="EPSG:4326", transform=from_origin(-120, 45, 0.01, 0.01), nodata=255,
) as dst:
    dst.write(np.zeros((6, 8, 8), dtype="uint8"))

item = {
    "type": "Feature", "stac_version": "1.0.0", "id": "known-bad-band-count",
    "geometry": None, "bbox": [-120, 44.92, -119.92, 45], "properties": {}, "links": [],
    "assets": {"data": {
        "href": "six_band.tif", "type": "image/tiff; application=geotiff", "roles": ["data"],
        "proj:code": "EPSG:4326", "proj:shape": [8, 8],
        "proj:transform": [0.01, 0, -120, 0, -0.01, 45],
        "proj:bbox": [-120, 44.92, -119.92, 45],
        "raster:bands": [{"name": "pfg", "data_type": "uint8", "nodata": 255}],
    }},
}
(out / "item.json").write_text(json.dumps(item, indent=2))
print(out / "item.json")
