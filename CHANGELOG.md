# Changelog

## Unreleased (post-RC integration candidate, 0.4.0.dev0)

### Checks
- `EO_BAND_COUNT_MISMATCH` (WARN): asset-level `eo:bands` length vs raster band count, only when the asset has no `bands`/`raster:bands`. Item-level `eo:bands` is never compared per asset; trailing alpha bands are ignored.
- `DUPLICATE_HREF_DIFFERENT_BANDS` (WARN): a duplicate-href group whose assets declare different band names, common names or `sar:polarizations`. It replaces the generic `DUPLICATE_DATA_HREF` for that group.
- `FILE_SIZE_MISMATCH` (WARN, opt-in via `--check-file-size` / `check_file_size=True`): asset `file:size` vs local file size or HTTP `Content-Range` total. Nothing is reported when the size cannot be determined unambiguously.
- Not implemented (on hold): `file:checksum` verification (needs a full download, and checks byte integrity rather than semantics) and `statistics` vs GDAL `STATISTICS_*` tags (approximate and optional).
- `NODATA_MISMATCH` compares float32 bands at float32 precision (FP-3, found on a live NRP catalog: STAC `-3.4e38` vs GDAL's float32 `-3.3999999521e38` was a false ERROR).

### Collections at scale
- Collection scale: `--sample first|random`, `--seed`, `--scan-limit`, `--page-size`, `--max-rps` / `--delay`, `--retries`. Bounded retries for transient STAC JSON failures (timeouts, 429 with `Retry-After`, 5xx); HTTP 202 empty-body WAF challenges are not retried. Per-run header cache so each asset href is opened once. Text and JSON output add `sampling` / `summary` (additive).
- **Default `--workers` lowered from 8 to 4** (also the `audit_collection` API default) to be gentler on public APIs.
- Items page / Item fetch failures no longer abort the collection audit: results are marked incomplete (`summary.complete: false`) and the CLI exits `2` unless an ERROR was found.

### Authenticated / signed assets
- Optional href resolver (`resolver=` in the Python API, `--resolver planetary-computer | alternate-<name> | identity | module:function` in the CLI). Default behaviour is unchanged.
- Planetary Computer SAS signing via the optional extra `[planetary-computer]`.
- `ASSET_RESOLVE_FAILED` (WARN; ERROR only with `--fail-unreadable`) when a resolver fails.
- Signing material (SAS, `X-Amz-*`, tokens, credentials in URLs, bearer headers) is redacted in every finding at construction, so no output format can echo it.

### Zarr (prototype, optional extra `[zarr]`)
- Zarr array metadata vs `proj:shape`, `data_type`, `nodata` (ERROR when the store was read and contradicts STAC). Scale/offset duplication with CF `scale_factor`/`add_offset` is `ZARR_DOUBLE_SCALING_RISK` / `ZARR_SCALE_CONFLICT` (WARN only). Without the extra: `ZARR_SUPPORT_UNAVAILABLE` WARN.

### GeoParquet (experimental, optional extra `[geoparquet]`)
- Footer-only checks of single-file Parquet assets: `TABLE_COLUMN_MISSING`, `TABLE_ROW_COUNT_MISMATCH`, `PRIMARY_GEOMETRY_MISSING` (ERROR only when declared on the asset itself; WARN when inherited), `CRS_MISMATCH` only for distinct EPSG codes. Type differences, case-only column differences, and partitioned/glob hrefs give WARN. `pyarrow>=14.0.1` (CVE-2023-47248).

### Reporting formats (CI)
- `--format text|json|sarif|junit` and `--output PATH` on `item` and `collection`. `--json` is kept as an alias of `--format json`. Exit codes are unchanged for every format.
- SARIF 2.1.0 output with a rule per finding code, stable `partialFingerprints`, and repo-relative artifact URIs, for GitHub code scanning.
- JUnit XML output with one testcase per Item: ERROR → failure, WARN → pass (failure under `--strict`), nothing inspected → skipped.
- JSON report: additive `schema_version` (`1.0`), `status`, `strict` and `summary` (counts by severity and code). JSON Schema in `docs/report.schema.json`. Documented in `docs/output-formats.md`.
- JSON `summary` combines collection counts with `by_severity` / `by_code`.

### Release engineering
- CI on Python 3.10–3.13 with lint (ruff), build, `twine check` and a clean-venv smoke install; tag-triggered release workflow using PyPI Trusted Publishing (not yet enabled). `docs/releasing.md`, `docs/release-checklist.md`.
- Offline benchmark/regression platform (`python -m benchmark.run`, manifest of live failure families, baseline diff). Not packaged.

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

### CLI and packaging
- `stac-integrity --version`.
- Single-source version (`stac_integrity.__version__`). Classifiers added. `test` extra.
- Exit codes: `0` clean, `1` semantic ERROR (or any warning with `--strict`), `2` operational failure.

### Tests
- Known-good and known-bad fixtures per invariant with explicit severities, CLI exit-code contract tests, and offline regression tests for every live-data fix.
