# stac-integrity-gate

An **asset-aware semantic integrity gate** for STAC raster data.

Normal STAC validators answer: **“Is this valid STAC JSON?”**

This tool asks: **“Does the STAC declaration match the data it points to?”**

## v0.2 checks

For supported raster assets (GeoTIFF/COG and JPEG2000 when Rasterio/GDAL can open them):

- `proj:code` / `proj:epsg` / `proj:wkt2` / `proj:projjson` vs raster CRS
- `proj:shape` vs `[height, width]`
- `proj:transform` vs raster affine transform
- `proj:bbox` vs raster bounds
- `bands` (STAC 1.1 / Raster v2) or `raster:bands` (Raster v1) count vs actual band count
- `data_type` vs actual dtype
- `nodata` vs actual band nodata
- `scale` / `offset` vs raster header as **warnings** only
- duplicate data-asset hrefs as **warnings**

It audits both:

- Item assets
- STAC 1.1 Collection-level assets

It intentionally does **not** compare Item geometry or `gsd` as hard invariants yet; both can be semantically valid without being identical to a raster’s rectangular footprint or pixel resolution.

## False-positive controls

- By default only assets with role `data` (or no roles) are audited. Use `--all-raster-assets` for visual/overview assets.
- Remote assets that cannot be opened are `WARN`, not semantic `ERROR`. Use `--fail-unreadable` if accessibility is part of your gate.
- `Collection.item_assets` is **not inherited** into Items. STAC requires applicable definition fields to be repeated on the Item asset; absent fields do not apply.
- Spatial comparisons use a default `0.01` pixel tolerance.

## Install

```bash
python -m pip install -e .
```

## One Item

```bash
stac-integrity item path/to/item.json
stac-integrity item https://example.org/item.json --json
stac-integrity item item.json --asset data
```

Backward-compatible v0.1 syntax also works:

```bash
stac-integrity item.json
```

## Collection / STAC API

```bash
stac-integrity collection collection.json --limit 100
stac-integrity collection https://example.org/collections/foo --limit 20 --json --summary-only
```

Collection mode checks Collection-level assets and then member Items (static `rel=item` links or a STAC API `rel=items` endpoint).

## Exit codes

- `0`: no semantic errors
- `1`: semantic mismatch found
- `2`: operational/input failure

Use `--strict` to make warnings fail CI.

## Known-bad regression

```bash
stac-integrity collection demo_collection_bad/collection.json
```

Expected:

```text
FAIL known-bad-collection-band-count | collection-assets: FAIL | ...
findings: BAND_COUNT_MISMATCH=1
```

The synthetic fixture mirrors a real failure class: STAC declares one raster band while the actual raster contains six.

## Public benchmark

On a machine with normal outbound network access:

```bash
python scripts/public_benchmark.py
```

Targets include Element84 Earth Search, CDSE, and a known-bad public NRP catalog. Results are written to:

- `benchmark/results/report.json`
- `benchmark/results/report.md`

This initial benchmark is a cross-provider smoke test, **not** a prevalence estimate.

## Why this exists

See [EVIDENCE.md](EVIDENCE.md). The evidence set includes independent failures from Element84 Earth Search, Microsoft Planetary Computer, Open Data Cube, openEO, EOPF, and a production data-workflows catalog whose existing structural validator reported CLEAN while never opening COG assets.

## Current boundary

v0.2 does not yet inspect:

- Zarr internal metadata
- GeoParquet schemas/data
- authenticated/signed asset workflows such as provider-specific URL signing
- scientific value-domain correctness
- randomized prevalence across a whole public catalog

Those are only worth adding if the live benchmark shows enough current value.
