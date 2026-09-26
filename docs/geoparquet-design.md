# GeoParquet / Table semantic integrity — design (Track F, experimental)

Status: prototype behind the optional extra `stac-integrity-gate[geoparquet]`.
Implementation: `stac_integrity/geoparquet_checks.py`. Tests: `tests/test_geoparquet.py`.
Evidence: `research/geoparquet-evidence/` (NRP public-data catalog sweep, 2026-09-26).

## Scope

Only the **Parquet footer** is read: Arrow schema, `num_rows`, and the key/value
metadata (`geo`). No row groups are downloaded. Remote `http(s)://` footers are
read with a small built-in HTTP Range reader (one suffix request of 64 KiB,
plus one more if the footer is larger); `s3://`/`gs://` are delegated to
pyarrow's filesystems; anything that fails is an access problem.

Asset dispatch (`audit_item_dict`): an asset that is *not* a raster and is a
Parquet asset (media type `application/vnd.apache.parquet`,
`application/x-parquet`, `application/parquet`, `…geoparquet…`, or
`.parquet`/`.geoparquet` path) and passes the data-role filter goes to
`geoparquet_checks.audit_parquet_asset`. Raster dispatch is unchanged.

## Guiding rule

ERROR requires three things: (1) the footer was actually read, (2) the value
compared is exact in the file format, and (3) the declaration was made **on the
asset itself**. Declarations inherited from Item `properties` (or the
Collection, for Collection assets) may describe a whole logical table spread
over several assets, so the same contradiction is WARN there. The NRP sweep
confirms this: `pad-us` declares Collection-level `table:columns` that include
columns absent from its `fee`/`easement` Parquet assets (10 WARNs, plausibly a
shared schema across assets rather than a publishing bug).

## Invariants

| Code | Declaration vs file | Severity | Rationale |
|---|---|---|---|
| `TABLE_COLUMN_MISSING` | `table:columns[].name` absent from the Arrow schema (case-insensitively too, not a partition key, not a nested `a.b` path whose `a` exists) | ERROR on asset / WARN inherited | Column names are exact in Parquet. A declared column that does not exist breaks any consumer query built from STAC. |
| `TABLE_COLUMN_CASE_MISMATCH` | name matches only case-insensitively | WARN | Arrow/pandas are case-sensitive, DuckDB/SQL often are not. Real case: NRP `vme.parquet` `surface` vs `SURFACE`. |
| `TABLE_COLUMN_PARTITION_KEY` | declared name is a Hive key in the href (`…/h0=5/…`) | WARN | Partition columns live in the path, not the file schema. |
| (none) | extra undeclared columns | no finding | Table ext does not require listing every column (28/257 NRP files have extras). |
| `TABLE_COLUMN_TYPE_MISMATCH` | declared `type` family ≠ Arrow type family | WARN | The Table extension type is free-form ("we recommend" Parquet types). Families: integer, float, decimal, number(=any numeric), string, boolean, date, timestamp, time, binary, nested. Unknown vocabulary (e.g. `geometry`) and geometry columns are never compared. Width differences inside a family (int16 vs int32) are ignored. |
| `TABLE_ROW_COUNT_MISMATCH` | `table:row_count` ≠ footer `num_rows` | ERROR on asset / WARN inherited | `num_rows` is exact and cheap. Real case: NRP `blm-sma.parquet` declares 439 200, footer has 865 059. |
| `TABLE_ROW_COUNT_INVALID` | non-integer `table:row_count` | WARN | Not comparable. |
| `PRIMARY_GEOMETRY_MISSING` | `table:primary_geometry` column absent | ERROR on asset / WARN inherited | Exact column name. |
| `PRIMARY_GEOMETRY_NOT_GEOMETRY` | column exists but is not in `geo.columns` | WARN | Could be WKB without GeoParquet metadata; the file is readable, only undocumented. |
| `PRIMARY_GEOMETRY_DIFFERS` | column is a geometry column but ≠ `geo.primary_column` | WARN | GeoParquet allows several geometry columns; which is "primary" is a convention. |
| `GEOPARQUET_METADATA_MISSING` | asset claims geometry (`table:primary_geometry` or geoparquet media type) but no `geo` key | WARN | Plain Parquet with WKB is common; not a proven contradiction. |
| `GEOPARQUET_METADATA_INVALID` | `geo` is not a JSON object | WARN | |
| `CRS_MISMATCH` | `proj:code`/`proj:epsg`/`proj:wkt2`/`proj:projjson` vs `geo` CRS of the primary column; both resolve to **distinct EPSG codes** | ERROR on asset only | Only when equivalence is certain to fail. |
| `CRS_MISMATCH_UNCERTAIN` | not equal, but no clean EPSG pair or inherited declaration | WARN | |
| `GEOPARQUET_CRS_UNDEFINED` | `geo` crs is JSON `null` | WARN | Spec: null = undefined CRS. |
| `CRS_UNPARSEABLE` / `GEOPARQUET_CRS_UNPARSEABLE` | cannot parse | WARN | |
| `PARQUET_PARTITIONED_UNVERIFIED` | href contains `* ? [` or ends with `/` | WARN, asset skipped | 481/1010 NRP table assets are hive globs (`hex/h0=*/data_0.parquet`). Expanding globs means listing object stores; out of MVP scope. |
| `GEOPARQUET_DEPENDENCY_MISSING` | pyarrow not installed | WARN, asset skipped | Never ERROR. |
| `ASSET_UNREADABLE` | footer read failed | WARN (ERROR only for a missing local file in a local catalog — same rule as rasters) | Access failures are not evidence. |

### CRS semantic caution

GeoParquet coordinates are always stored x/y (lon/lat) regardless of the CRS
definition's axis order, and a missing `crs` key means `OGC:CRS84`. Rasterio
says `OGC:CRS84 != EPSG:4326`. Without special handling the NRP
`nwi-v2.parquet` (declares `proj:epsg: 4326`, `geo` has no crs) would be a
false ERROR. The check folds `OGC:CRS84/83/27` onto `EPSG:4326/4269/4267`
before comparing EPSG codes. 129 NRP files carry an EPSG:4326 PROJJSON, 100
omit crs; the sweep produced no CRS findings.

### Not implemented (deliberately)

- `geo.columns[].bbox` vs Item `bbox`/`proj:bbox`: optional, and Item bbox can
  cover several assets; needs a tolerance model → future WARN at most.
- `geometry_types` vs anything in STAC: STAC has no standard field.
- Hive/glob datasets: would need object-store listing and per-partition reads.
- Row-group statistics or pixel/row values: never read.

## Dependency

`geoparquet = ["pyarrow>=14.0.1"]`. 14.0.1 fixes CVE-2023-47248 (arbitrary
code execution when reading untrusted Parquet/IPC via `PyExtensionType`);
remote catalog assets are untrusted input. pyarrow is imported only inside
functions (`pyarrow_available()`, `_open_footer`), so the core package imports
and runs without it. `fsspec` is **not** used by the package (only by the
research script).

## False-positive risk

- Stale-but-harmless row counts (the dataset was re-exported with more rows):
  still a contradiction a consumer would act on, so ERROR is kept. One case in
  the NRP sweep, and it is a genuine metadata drift.
- Asset-level `table:columns` copied from a sibling asset (e.g. PMTiles
  descriptions reused for Parquet): would give ERROR if a column is really
  absent. None observed in 257 files; name checks produced 0 ERRORs.
- Type vocabulary: 61 WARNs across the NRP sweep, mostly publisher-declared
  `double` for int columns (CDC SVI) and `int32`/`date` for string columns.
  WARN only.
