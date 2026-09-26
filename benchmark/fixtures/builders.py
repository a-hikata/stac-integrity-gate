"""Deterministic offline fixtures for real-world failure families.

Each builder writes a *minimal* GeoTIFF (a few pixels) plus the STAC JSON that
reproduces the shape of a finding observed in a live public catalog (Waves 1-3).
Nothing binary is committed: fixtures are generated at run time into a scratch
directory. Grids are scaled down but keep the ratios and coefficients that
matter (e.g. 20 m declared vs 60 m actual, 1e-05 vs 9.259e-05 pixel size,
off-by-one shape). Identifiers and internal paths are anonymized.

A builder returns a :class:`FixtureSpec` describing how to run the CLI on it.
Register new builders with the ``@fixture("<builder-name>")`` decorator and
reference the name from ``benchmark/manifest.json`` (``fixture.builder``).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np
import rasterio
from rasterio.transform import Affine, from_origin

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class FixtureSpec:
    """How the runner should audit a built fixture.

    command:   CLI sub-command, ``item`` or ``collection``.
    source:    path (or URL) passed to the CLI.
    documents: URL -> local JSON path. The runner serves these instead of the
               network, so a fixture can pretend to be a remote catalog while
               staying offline (any other URL fetch fails the case).
    cli_args:  extra CLI arguments (e.g. ``["--fail-unreadable"]``).
    """

    command: str
    source: str
    documents: dict[str, str] = field(default_factory=dict)
    cli_args: list[str] = field(default_factory=list)


Builder = Callable[[Path], FixtureSpec]
BUILDERS: dict[str, Builder] = {}


def fixture(name: str) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        if name in BUILDERS:
            raise ValueError(f"duplicate fixture builder: {name}")
        BUILDERS[name] = fn
        return fn

    return register


def build(name: str, workdir: Path) -> FixtureSpec:
    if name not in BUILDERS:
        raise KeyError(f"unknown fixture builder: {name!r} (known: {', '.join(sorted(BUILDERS))})")
    workdir.mkdir(parents=True, exist_ok=True)
    return BUILDERS[name](workdir)


# --- helpers ---------------------------------------------------------------

def write_tif(
    path: Path,
    *,
    crs: str,
    transform: Affine,
    width: int,
    height: int,
    count: int = 1,
    dtype: str = "float32",
    nodata: float | None = None,
    scales: list[float] | None = None,
    offsets: list[float] | None = None,
    tags: dict[str, str] | None = None,
) -> Path:
    kwargs: dict[str, Any] = {}
    if nodata is not None:
        kwargs["nodata"] = nodata
    with rasterio.open(
        path, "w", driver="GTiff", width=width, height=height, count=count,
        dtype=dtype, crs=crs, transform=transform, **kwargs,
    ) as dst:
        # Deterministic content; values are irrelevant to the header checks.
        data = np.arange(count * height * width, dtype="float64").reshape(count, height, width)
        dst.write(data.astype(dtype))
        if scales is not None:
            dst.scales = tuple(scales)
        if offsets is not None:
            dst.offsets = tuple(offsets)
        if tags:
            dst.update_tags(**tags)
    return path


def stac_item(item_id: str, assets: dict[str, dict[str, Any]], properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "Feature",
        "stac_version": "1.1.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/projection/v2.0.0/schema.json",
            "https://stac-extensions.github.io/raster/v1.1.0/schema.json",
        ],
        "id": item_id,
        "geometry": None,
        "properties": {"datetime": "2020-01-01T00:00:00Z", **(properties or {})},
        "links": [],
        "assets": assets,
    }


def geotiff_asset(href: str, **fields: Any) -> dict[str, Any]:
    return {"href": href, "type": "image/tiff; application=geotiff; profile=cloud-optimized",
            "roles": ["data"], **fields}


def write_json(path: Path, doc: dict[str, Any]) -> Path:
    path.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return path


def _transform_list(t: Affine) -> list[float]:
    return [t.a, t.b, t.c, t.d, t.e, t.f]


# --- 1. Element84 Earth Search legacy sentinel-2-l2a `aot` --------------------
# Live: proj:shape 5490x5490 / 20 m declared, header is 1830x1830 / 60 m.
# Minimized 1/305: declared 18x18 @ 20 m, actual 6x6 @ 60 m (same 3:1 ratio).

@fixture("e84_s2_legacy_aot")
def e84_s2_legacy_aot(d: Path) -> FixtureSpec:
    t = from_origin(300000, 9200020, 60, 60)
    write_tif(d / "aot.tif", crs="EPSG:32753", transform=t, width=6, height=6, dtype="uint16", nodata=0)
    item = stac_item(
        "S2X_00XXX_20240709_0_L2A",
        {"aot": geotiff_asset(
            "aot.tif",
            **{"proj:shape": [18, 18], "proj:transform": [20, 0, 300000, 0, -20, 9200020],
               "raster:bands": [{"data_type": "uint16", "nodata": 0, "scale": 0.001, "offset": 0}]},
        )},
        {"proj:epsg": 32753},
    )
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


# --- 2-4. Planetary Computer 3dep-seamless ------------------------------------

_3DEP_T = from_origin(-105.0005556, 31.0005556, 9.259259e-05, 9.259259e-05)


@fixture("pc_3dep_2013_pixel_size")
def pc_3dep_2013_pixel_size(d: Path) -> FixtureSpec:
    # 2013-derived items declare pixel size 1e-05; header is 1/3" = 9.259e-05.
    write_tif(d / "dem.tif", crs="EPSG:4269", transform=_3DEP_T, width=4, height=3, nodata=-9999)
    item = stac_item("n00w000-13", {"data": geotiff_asset(
        "dem.tif", **{"proj:shape": [3, 4], "proj:transform": [1e-05, 0, -105.0005556, 0, -1e-05, 31.0005556]})},
        {"proj:epsg": 5498})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


@fixture("pc_3dep_compound_crs")
def pc_3dep_compound_crs(d: Path) -> FixtureSpec:
    # NAD83 + NAVD88 compound EPSG:5498 declared; header is horizontal EPSG:4269.
    write_tif(d / "dem.tif", crs="EPSG:4269", transform=_3DEP_T, width=4, height=3, nodata=-9999)
    item = stac_item("n00w000-1", {"data": geotiff_asset(
        "dem.tif", **{"proj:shape": [3, 4], "proj:transform": _transform_list(_3DEP_T)})},
        {"proj:epsg": 5498})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


@fixture("pc_3dep_2019_shape")
def pc_3dep_2019_shape(d: Path) -> FixtureSpec:
    # Live: proj:shape 10813 declared vs 10812 actual. Minimized: 13 vs 12.
    write_tif(d / "dem.tif", crs="EPSG:4269", transform=_3DEP_T, width=12, height=12, nodata=-9999)
    item = stac_item("n00w000-13", {"data": geotiff_asset(
        "dem.tif", **{"proj:shape": [13, 13], "proj:transform": _transform_list(_3DEP_T)})},
        {"proj:epsg": 5498})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


# --- 5. NASA GHG Center: float proj:epsg --------------------------------------

@fixture("nasa_ghg_float_epsg")
def nasa_ghg_float_epsg(d: Path) -> FixtureSpec:
    t = from_origin(-180, 90, 1, 1)
    write_tif(d / "flux.tif", crs="EPSG:4326", transform=t, width=4, height=3, nodata=-9999)
    item = stac_item("ghg-fixture", {"data": geotiff_asset(
        "flux.tif", **{"proj:epsg": 4326.0, "proj:shape": [3, 4], "proj:transform": _transform_list(t)})})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


# --- 6-7. Digital Earth Australia (Wave 3 blind audit) ------------------------

@fixture("dea_nidem_crs_nodata")
def dea_nidem_crs_nodata(d: Path) -> FixtureSpec:
    # proj:code EPSG:4326 declared while the GeoTIFF is GDA94 / Australian Albers,
    # and STAC nodata -9999 while the header carries no nodata tag.
    t = from_origin(-1785175, -3178425, 25, 25)
    write_tif(d / "nidem.tif", crs="EPSG:3577", transform=t, width=5, height=4, nodata=None)
    item = stac_item("00000000-0000-0000-0000-000000000000", {"nidem": geotiff_asset(
        "nidem.tif", **{"proj:code": "EPSG:4326", "proj:shape": [4, 5],
                        "proj:transform": _transform_list(t),
                        "raster:bands": [{"data_type": "float32", "nodata": -9999}]})})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


@fixture("dea_mstp_shape")
def dea_mstp_shape(d: Path) -> FixtureSpec:
    # Live: proj:shape 1199 declared vs 1200 actual. Minimized: 4 vs 5.
    t = from_origin(134, -23, 0.2, 0.2)
    write_tif(d / "mstp.tif", crs="EPSG:4326", transform=t, width=5, height=5)
    item = stac_item("00000000-0000-0000-0000-000000000001", {"mstp": geotiff_asset(
        "mstp.tif", **{"proj:code": "EPSG:4326", "proj:shape": [4, 4], "proj:transform": _transform_list(t)})})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


# --- 8. DLR terrabyte: remote catalog, publisher-internal file:// href -------

REMOTE_ITEM_URL = "https://stac.example.invalid/collections/fixture/items/remote-file-href"


@fixture("dlr_terrabyte_file_href")
def dlr_terrabyte_file_href(d: Path) -> FixtureSpec:
    # The href points at the publisher's filesystem; it cannot exist here.
    item = stac_item("remote-file-href", {"B02": geotiff_asset("file:///dss/publisher-internal/fixture/B02.tif")})
    path = write_json(d / "item.json", item)
    return FixtureSpec("item", REMOTE_ITEM_URL, documents={REMOTE_ITEM_URL: str(path)})


# --- 9. NRP rap-pfg-cover (historical): 1 declared band vs 6-band COG --------
# Reuses the repository's committed demo fixtures (tiny 8x8 six-band COG).

@fixture("nrp_rap_pfg_item")
def nrp_rap_pfg_item(d: Path) -> FixtureSpec:
    return FixtureSpec("item", str(REPO_ROOT / "demo_bad" / "item.json"))


@fixture("nrp_rap_pfg_collection")
def nrp_rap_pfg_collection(d: Path) -> FixtureSpec:
    return FixtureSpec("collection", str(REPO_ROOT / "demo_collection_bad" / "collection.json"))


# --- 10. Clean controls -------------------------------------------------------

@fixture("e84_s2_c1_red_control")
def e84_s2_c1_red_control(d: Path) -> FixtureSpec:
    # Collection-1 red: 10 m UTM grid, scale/offset stored in the header too.
    t = from_origin(399960, 4200000, 10, 10)
    write_tif(d / "red.tif", crs="EPSG:32610", transform=t, width=8, height=8, dtype="uint16", nodata=0,
              scales=[0.0001], offsets=[-0.1])
    item = stac_item("S2X_10XXX_20240709_0_L2A", {"red": geotiff_asset(
        "red.tif", **{"proj:code": "EPSG:32610", "proj:shape": [8, 8], "proj:transform": _transform_list(t),
                      "proj:bbox": [399960, 4199920, 400040, 4200000],
                      "raster:bands": [{"data_type": "uint16", "nodata": 0, "scale": 0.0001, "offset": -0.1}]})})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))


@fixture("cop_dem_pixel_is_point_control")
def cop_dem_pixel_is_point_control(d: Path) -> FixtureSpec:
    # Copernicus DEM GLO-30 tiles are PixelIsPoint. The declaration follows the
    # GDAL (area-convention) geotransform, which must not produce a finding.
    t = from_origin(10.0 - 1 / 7200, 46.0 + 1 / 7200, 1 / 3600, 1 / 3600)
    path = write_tif(d / "dem.tif", crs="EPSG:4326", transform=t, width=6, height=6,
                     tags={"AREA_OR_POINT": "Point"})
    with rasterio.open(path) as src:
        if src.tags().get("AREA_OR_POINT") != "Point":
            raise RuntimeError("fixture did not persist AREA_OR_POINT=Point")
    # Declare the grid we wrote (independently of what GDAL reads back): a
    # half-pixel PixelIsPoint shift on read would surface as a regression here.
    bounds = [t.c, t.f + 6 * t.e, t.c + 6 * t.a, t.f]
    item = stac_item("Copernicus_DSM_COG_10_N00_00_E000_00_DEM", {"data": geotiff_asset(
        "dem.tif", **{"proj:code": "EPSG:4326", "proj:shape": [6, 6], "proj:transform": _transform_list(t),
                      "proj:bbox": bounds, "raster:bands": [{"data_type": "float32"}]})})
    return FixtureSpec("item", str(write_json(d / "item.json", item)))
