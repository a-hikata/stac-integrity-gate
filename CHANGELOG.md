# Changelog

## 0.3.0rc1 — release candidate (unreleased)

First public release candidate.

### Checks
- Compares STAC raster declarations with the actual raster: CRS (`proj:code` / `proj:epsg` / `proj:wkt2` / `proj:projjson`), `proj:shape`, `proj:transform`, `proj:bbox`, band count, per-band `data_type` and `nodata` (ERROR), and `scale` / `offset` (WARN).
- Supports STAC 1.1 `bands` and raster extension v1 `raster:bands`.
- Duplicate data-asset href warning.

### Collections
- `stac-integrity collection`: audits STAC 1.1 Collection-level assets and member Items, using static `rel=item` links or STAC API `rel=items` paging.
- `Collection.item_assets` is not inherited into Items.

### Remote assets
- Object-store URIs (`s3://`, `gs://`, …) are treated as remote. When they cannot be opened they produce `ASSET_UNREADABLE` WARN, not ERROR. Previously an unreadable `s3://` asset was escalated as if it were a missing local file and exited `1`.
- The public benchmark harness defaults to `AWS_NO_SIGN_REQUEST=YES` for public buckets.

### False-positive fixes backed by live benchmarks
- **Compound CRS normalization:** a declared compound CRS whose horizontal part equals the 2D raster CRS (e.g. `EPSG:5498` vs `EPSG:4269`, Planetary Computer `3dep-seamless`) is now `CRS_VERTICAL_UNVERIFIED` (WARN) instead of a `CRS_MISMATCH` ERROR.
- **Float EPSG normalization:** an integral float `proj:epsg` such as `4326.0` (NASA GHG Center) is compared as EPSG:4326. Previously it was reported as `CRS_UNPARSEABLE` and the CRS check was skipped.

### CLI and packaging
- `stac-integrity --version`.
- Single-source version (`stac_integrity.__version__`). Classifiers added. `test` extra.
- Exit codes: `0` clean, `1` semantic ERROR (or any warning with `--strict`), `2` operational failure.

### Tests
- Known-good and known-bad fixtures per invariant with explicit severities, CLI exit-code contract tests, and offline regression tests for every live-data fix.
