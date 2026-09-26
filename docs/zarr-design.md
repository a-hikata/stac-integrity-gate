# Zarr semantic integrity — scope and invariants (Track E)

Status: prototype (`stac_integrity/zarr_checks.py`, optional extra `.[zarr]`).

The gate's principle is unchanged: **ERROR only when the asset was read and its
metadata demonstrably contradicts the STAC declaration; anything whose meaning
depends on client behaviour or on an unstated convention is WARN; access
failures follow the existing `ASSET_UNREADABLE` rules.**

Only Zarr *metadata* is read (`zarr.json`, or `.zarray` / `.zattrs` / `.zgroup`).
No chunk data is ever fetched.

## 1. How a STAC asset maps to Zarr

| STAC asset `href` points to | Seen in the wild | Prototype behaviour |
|---|---|---|
| a Zarr **array** (`…/r10m/b04`) | EOPF `sentinel-2-l2a` (Zarr v2, CPM 2.7.0): one asset per band | compared directly |
| a Zarr **group** whose child arrays are named by `bands[].name` (`…/r10m` + bands `b02…b08`) | EOPF `sentinel-2-l2a-zarr3` (Zarr v3, CPM 3.0.0) | each band name is resolved to a child array (exact name, else a *unique* case-insensitive match) and compared |
| a group with bands / band-level fields declared, none resolvable | EOPF Explorer S1 RTC `…/ascending` (bands `vv`,`vh` live one level deeper, in GeoZarr multiscale level `r10m/`) | WARN `ZARR_GROUP_UNRESOLVED`; nothing compared |
| a group that declares nothing comparable | whole-store `product` / `zarr-store` assets | no finding (counted as checked) |

Band→array resolution opens the child path directly (`name`, then lower-case,
then upper-case) instead of listing the group, because plain-HTTPS object stores
cannot list (EOPF Sample Service eopf-stac #34; zarr's `array_keys()` returns
`[]` against `data.eodc.eu`). Listing, when available, is only used for a
*unique* case-insensitive fallback. EOPF CPM 2.7.0 declares `B04` while the
array is `b04`, so case folding is required in practice.

GeoZarr `multiscales` groups (attribute `multiscales.layout[*].asset`) are not
descended into: which level the STAC fields describe is not stated anywhere, so
guessing `layout[0]` was left as a follow-up.

Asset detection (`is_zarr_asset`): media type contains `zarr`
(`application/vnd+zarr`, `application/vnd.zarr; version=3`, `application/x-zarr`)
or the href path ends in `.zarr` / contains `.zarr/`. Zarr detection runs before
the raster detection, so a Zarr asset is never handed to Rasterio.

Things the prototype deliberately does **not** interpret (ambiguous):

- `xarray:open_kwargs` / `xarray:open_dataset_kwargs` (`group=`, `engine=`,
  `op_mode=`): these describe how *one* client opens the store; the variable a
  client ends up with depends on the engine (e.g. `eopf-zarr` native mode
  renames variables). Not used for resolution.
- Datacube extension `cube:variables` / `cube:dimensions`: would allow
  per-variable shape/dimension checks on cube items, but no live EOPF item
  currently carries it (EOPF Explorer data-model #195 *requests* it). Deferred.
- `zarr:node_type`, `zarr:consolidated` (STAC zarr extension v1.1): could be
  checked against the opened node type; low value, deferred.
- Band-count equality for group hrefs: groups legitimately contain extra arrays
  (coordinates `x`/`y`, masks, `spatial_ref`), so "group has more arrays than
  `bands`" is not a contradiction.

## 2. Comparable facts and invariants

Notation: *declared* = STAC value (band first, then asset-level for
STAC 1.1 / raster v2 inheritance, then item properties for `proj:*` exactly as
the GeoTIFF path does); *stored* = Zarr array metadata.

### I1 — shape (`proj:shape` vs array shape)
- `proj:shape` is `[rows, cols]`. It is compared to the array's last two
  dimensions when the array is 2-D, or when its last two dimension names
  (v3 `dimension_names`, v2 xarray `_ARRAY_DIMENSIONS`) are a known spatial pair
  (`y/x`, `lat/lon`, `latitude/longitude`, `rows/cols`).
- Mismatch → **ERROR `SHAPE_MISMATCH`** (same code as GeoTIFF path).
- Any other layout (1-D, `(time, band, z)`, unnamed N-D) → WARN
  `ZARR_SHAPE_UNVERIFIED`.

### I2 — data type (`data_type` vs stored dtype)
- STAC raster `data_type` describes stored pixel values. Stored dtype is the
  array dtype (normalised through numpy, so `<u2` == `uint16`).
- Mismatch → **ERROR `DATA_TYPE_MISMATCH`**, **except**: declared type is
  floating, stored is integer, and the array carries CF decoding attributes
  (`scale_factor`, `add_offset` or `_FillValue`). Then the declaration plausibly
  describes the xarray-*decoded* values (EOPF data-pipeline #384 raises exactly
  this question) → WARN `ZARR_DATA_TYPE_DECODED`.

### I3 — nodata (`nodata` vs `_FillValue` / `fill_value`)
Zarr's `fill_value` is the value of *uninitialised chunks* — every Zarr array
has one (often `0` by default) whether or not it is semantic nodata. CF
`_FillValue` is the semantic missing-value marker (xarray masks it).
- Store has a `_FillValue` attribute and it differs from declared `nodata`
  → **ERROR `NODATA_MISMATCH`**.
- No `_FillValue` attribute, array `fill_value` differs from declared `nodata`
  → WARN `ZARR_FILL_VALUE_DIFFERS` (fill_value may be only a storage default;
  xarray on Zarr v2 treats it as `_FillValue`, so this is worth a look).
- STAC declares nodata, store has neither → WARN `ZARR_NODATA_NOT_IN_STORE`.
- `nan`/`"nan"`/`inf` strings are normalised as in the GeoTIFF path.

### I4 — scale/offset and double scaling (E5)
Declared STAC scaling: band `scale`/`offset` (raster v1 `raster:bands`),
band `raster:scale`/`raster:offset`, then asset `raster:scale`/`raster:offset`
(raster v2). Stored scaling: CF attributes `scale_factor` / `add_offset` on the
array. A side is *active* when scale ≠ 1 or offset ≠ 0 (a missing member defaults
to 1 / 0). Identity scaling (`scale 1, offset 0`, e.g. EOPF `SCL_20m`) is a
no-op and never reported.

| STAC active | CF active | values | finding |
|---|---|---|---|
| yes | yes | equal | WARN `ZARR_DOUBLE_SCALING_RISK` |
| yes | yes | differ | WARN `ZARR_SCALE_CONFLICT` |
| yes | no | — | none (STAC is the only description — the COG-like pattern) |
| no | yes | — | none (self-describing store — the fix proposed in #82) |

Why never ERROR:
- *Equal values* are not a contradiction: both documents describe the same
  raw→physical mapping of the same stored integers. The defect is the
  *composition* by clients: xarray (`mask_and_scale=True`, the default) decodes
  CF attributes, and a raster-extension client (titiler-openeo ≥ 0.17 by
  default) applies `raster:scale` again on top — EOPF #82 shows reflectance
  collapsing onto ≈ −0.1. A client that reads raw values (Zarr/GDAL raw access)
  and applies only STAC is correct. Whether the item is broken therefore
  depends on the consumer, which the gate cannot see.
- *Differing values* are more suspicious, but still not provably wrong: the
  raster extension has no field saying whether its scale applies to raw or to
  already-decoded values, so a STAC scale that intentionally describes a second
  stage on top of CF decoding is expressible and cannot be distinguished from an
  error. WARN with both values attached.

### I5 — CRS (design only, not implemented)
Comparable sources in the store: zarr-conventions `proj:code` / `proj:wkt2` on
the group (EOPF v3 groups carry `proj:code: EPSG:32626`), CF `grid_mapping`
→ sibling variable (`spatial_ref`) with `crs_wkt` / `spatial_ref` / `epsg_code`
attributes, GeoZarr `spatial:transform` / `spatial:shape`. Resolution requires
walking to parents/siblings, and several conventions coexist in 2026; the
expected yield (EOPF values matched in the live check) did not justify it for
the MVP.

### I6 — scope/identity (EOPF Explorer #195)
Two different data assets pointing at the same group are already reported by
the existing metadata-only `DUPLICATE_DATA_HREF` WARN. A Zarr-aware extension
(e.g. "asset declares one `datetime` but the array has a `time` dimension of
length N") needs `cube:dimensions` or dimension coordinates and is deferred.

## 3. Dependency design (E3)

- Extra: `zarr = ["zarr>=2.16", "fsspec>=2023.6", "aiohttp"]`.
  - `zarr>=2.16`: zarr-python 3.x reads **both** Zarr v2 and v3 stores but
    needs Python ≥ 3.11; the project supports 3.10, where pip resolves
    zarr 2.18 (v2 stores only). Every API used (`zarr.open(mode="r")`,
    `Array.shape/dtype/fill_value/attrs`, `Group.array_keys()`, `group[name]`)
    exists in both major versions. No upper bound (no known break; CI will say).
  - `fsspec` + `aiohttp`: public catalogs serve stores over HTTPS; zarr needs
    fsspec's HTTP filesystem for that. `s3://`/`gs://` need `s3fs`/`gcsfs`,
    which are left to the user (an open failure is a WARN, see below).
- `zarr` is imported lazily inside `zarr_checks` only. Core install stays
  `rasterio` only.
- Extra missing → each Zarr asset is counted as *skipped* and one
  WARN `ZARR_SUPPORT_UNAVAILABLE` per item lists them, with the install hint.
- Open failure → `ASSET_UNREADABLE`, with the same severity rules as rasters
  (ERROR only for a missing local store referenced by a local catalog).

## 4. Known false-positive risks

- Item-level `proj:shape` inherited by multi-resolution Zarr assets (same risk
  as the GeoTIFF path; EOPF puts `proj:shape` on the asset).
- Case-insensitive band→array resolution could pick a wrong array if a store
  has e.g. `B04` and `b04`; ambiguity (more than one case-insensitive match) is
  treated as unresolved.
- `ZARR_FILL_VALUE_DIFFERS` on stores that never intended fill_value as nodata
  (WARN only).
