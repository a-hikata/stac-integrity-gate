# Changelog

## 0.3.0rc1 — release candidate (unreleased)

First public release candidate.

### Checks
- Compares STAC raster declarations with the actual raster: CRS (`proj:code` / `proj:epsg` / `proj:wkt2` / `proj:projjson`), `proj:shape`, `proj:transform`, `proj:bbox`, band count, per-band `data_type` and `nodata` (ERROR when the header contradicts the declaration), and `scale` / `offset` (WARN).
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

### False-positive fixes from the Wave 3 blind audit
- **Remote catalogs with `file://` hrefs:** when a catalog fetched over HTTP(S) references `file://` or local-path assets that cannot be opened (e.g. DLR terrabyte's HPC-internal paths), the result is now `ASSET_UNREADABLE` **WARN** and exit `0`. Previously it was ERROR and exit `1`. A local catalog that references a missing local file is still an ERROR.
- **Nodata missing from the header:** STAC `nodata` with no nodata tag in the raster header is now `NODATA_NOT_IN_HEADER` **WARN** instead of a `NODATA_MISMATCH` ERROR (DEA `nidem`: STAC `-9999`, no header tag, but 99.4% of pixels are `-9999`). A header nodata that differs from the declaration is still `NODATA_MISMATCH` ERROR.
- Offline regression fixtures for every live-catalog finding (`tests/test_live_regressions.py`).

### Reporting formats (CI)
- `--format text|json|sarif|junit` and `--output PATH` on `item` and `collection`. `--json` is kept as an alias of `--format json`. Exit codes are unchanged for every format.
- SARIF 2.1.0 output with a rule per finding code, stable `partialFingerprints`, and repo-relative artifact URIs, for GitHub code scanning.
- JUnit XML output with one testcase per Item: ERROR → failure, WARN → pass (failure under `--strict`), nothing inspected → skipped.
- JSON report: additive `schema_version` (`1.0`), `status`, `strict` and `summary` (counts by severity and code). JSON Schema in `docs/report.schema.json`. Documented in `docs/output-formats.md`.

### CLI and packaging
- `stac-integrity --version`.
- Single-source version (`stac_integrity.__version__`). Classifiers added. `test` extra.
- Exit codes: `0` clean, `1` semantic ERROR (or any warning with `--strict`), `2` operational failure.

### Tests
- Known-good and known-bad fixtures per invariant with explicit severities, CLI exit-code contract tests, and offline regression tests for every live-data fix.
