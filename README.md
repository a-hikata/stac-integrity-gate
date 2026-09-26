# stac-integrity-gate

**Checks that the STAC metadata for a raster matches the raster file it points to.**

A STAC Item can be perfectly valid JSON, pass every schema check, and still describe a grid, band layout or CRS that the referenced GeoTIFF does not have. `stac-integrity-gate` opens the asset's header and compares it with what the STAC metadata declares. If they disagree, it fails your CI.

```text
$ stac-integrity item demo_bad/item.json
FAIL known-bad-band-count | checked: 1 | skipped: 0 | errors: 1 | warnings: 0
[ERROR] BAND_COUNT_MISMATCH asset=data field=bands: Declared band count does not match the raster band count.
  declared=1
  actual=6
```

> Status: **0.3.0rc1, release candidate.** Not yet published to PyPI.

## What problem this solves

Downstream loaders such as odc-stac and stackstac build their pixel grid from STAC `proj:*` metadata without opening every file. When that metadata is wrong, you get misplaced or wrongly sized arrays, often with no error at all. Live examples found during validation (see [docs/validation-evidence.md](docs/validation-evidence.md)):

- A public Sentinel-2 catalog declares a 20 m grid for an AOT asset that is actually 60 m.
- A public DEM catalog declares a pixel size of `1e-05°` for tiles whose real pixel size is `9.26e-05°`.
- A national data-cube catalog declares `EPSG:4326` for rasters that are actually in `EPSG:3577` (Albers metres).
- A published COG has 6 bands while its STAC metadata declares 1.

The first three passed structural STAC validation (PySTAC and `stac_valid`). The fourth was missed by the publisher's own structural validator, which never opened the COG.

## What existing STAC validators do

Schema validators (e.g. `stac-validator`, `stac_valid`, PySTAC `validate()`) check that the JSON matches the STAC core and extension schemas. Linters (e.g. `stac-check`) check best practices. COG validators (e.g. `rio cogeo validate`) check that a file is a well-formed Cloud-Optimized GeoTIFF.

None of these compare the **declared values** in STAC with the **actual values** in the raster.

## What this tool additionally checks

`stac-integrity-gate` is a **semantic integrity gate** that runs *after* schema validation, linting and COG validation. It does not replace them. For every selected raster asset it reads the header (not the pixels) and compares:

| STAC declaration | compared against |
|---|---|
| `proj:code` / `proj:epsg` / `proj:wkt2` / `proj:projjson` | raster CRS |
| `proj:shape` | `[height, width]` |
| `proj:transform` | raster affine transform |
| `proj:bbox` | raster bounds |
| number of `bands` (STAC 1.1) or `raster:bands` (raster extension v1) | raster band count |
| per-band `data_type` | raster dtype |
| per-band `nodata` | raster nodata (ERROR only if the header has a different nodata; WARN if it has none) |
| per-band `scale` / `offset` | raster scale / offset (warning only) |
| data assets sharing one href | (warning only) |

Asset fields take precedence. Otherwise the tool falls back to the Item's `properties`.

## Installation

Requires Python ≥ 3.10 and [rasterio](https://rasterio.readthedocs.io/) ≥ 1.3, which bundles GDAL in its wheels.

From a source checkout:

```bash
python -m pip install .
```

From a built wheel:

```bash
python -m pip install dist/stac_integrity_gate-0.3.0rc1-py3-none-any.whl
```

Check the install:

```bash
stac-integrity --version
# stac-integrity 0.3.0rc1
```

## Quick start

From a source checkout, run the bundled known-bad fixtures:

```bash
stac-integrity item demo_bad/item.json                          # exit 1
stac-integrity collection demo_collection_bad/collection.json   # exit 1
```

Audit your own catalog:

```bash
stac-integrity item path/to/item.json
stac-integrity item https://example.org/stac/item.json --asset data --json
stac-integrity collection path/to/collection.json --limit 100
stac-integrity collection https://example.org/stac/v1/collections/foo --limit 20 --json --summary-only
```

`stac-integrity path/to/item.json` (without a subcommand) is kept for backward compatibility and is treated as `item`.

## Example failure

A Collection whose STAC 1.1 Collection-level asset declares one band while the GeoTIFF has six:

```text
$ stac-integrity collection demo_collection_bad/collection.json
FAIL known-bad-collection-band-count | collection-assets: FAIL | items: 0 | failing-items: 0 | assets: 1 | errors: 1 | warnings: 0
findings: BAND_COUNT_MISMATCH=1
sampling: first | limit: 100 | selected: 0
summary: items: 0 | assets-opened: 1 | cache-hits: 0 | errors: 1 | warnings: 0 | unreadable: 0 | http: 0 req / 0 retries | complete: yes
```

`--json` prints the full result, including each finding's `severity`, `code`, `asset`, `field`, `declared` and `actual` values.

## Large collections: sampling and politeness

```bash
stac-integrity collection https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a \
  --limit 5 --sample random --seed 1 --asset red --max-rps 2
```

- `--sample first` (default) audits the first `--limit` Items in the publisher's order. `--sample random` draws a uniform reservoir sample of `--limit` Items from a **candidate pool of the first `--scan-limit` Items (default 1000)** in the publisher's order. The sample is uniform over the whole Collection only when the pool is exhausted (`sampling.pool_exhausted: true`); otherwise it is biased toward whatever the server lists first (often the newest Items). The seed is always reported; if `--seed` is omitted one is generated. The same seed reproduces the same sample only while the publisher's Item order and the pool are unchanged (live APIs that ingest new Items shift the pool). Random mode requests pages of 100 (`--page-size`) unless set.
- Stratified sampling is not offered: there is no generic, honest stratum definition across Collections (time, grid tile, platform are Collection-specific), and STAC API paging order is not guaranteed.
- `--workers` defaults to **4** (was 8) and also bounds concurrent raster header opens. `--max-rps N` / `--delay S` rate-limit STAC JSON requests and asset opens with a shared token bucket (one open may issue several HTTP range reads).
- STAC JSON fetches retry transient failures only (timeouts, connection resets, HTTP 429 honouring `Retry-After` up to 60 s, HTTP 5xx) with bounded exponential backoff (`--retries`, default 2). 4xx is not retried. HTTP 202 with an empty body (a WAF/bot challenge) is reported immediately and not retried. Remote raster opens use GDAL's own `GDAL_HTTP_MAX_RETRY=2`, `GDAL_HTTP_RETRY_DELAY=1`.
- Each resolved asset href is opened once per run; the header snapshot (or open failure) is reused for other Items referencing it.
- If an items page or Item document cannot be fetched, the audit continues with what it has, is marked `complete: no` / `summary.complete: false`, and exits `2` unless a semantic ERROR was found. Fetch failures are never reported as semantic findings.
- `--json` adds `sampling` and `summary` blocks; existing keys are unchanged.

## CI usage

Exit codes:

| exit | meaning |
|---:|---|
| `0` | no ERROR findings. Warnings are allowed unless `--strict` is set |
| `1` | at least one ERROR finding, or any WARN finding with `--strict` |
| `2` | operational/input failure: the STAC JSON could not be loaded or parsed, or the source is not a Collection; for `collection`, also an incomplete audit (items page / Item fetch failed) with no ERROR |

GitHub Actions example. The step fails when a semantic ERROR is found:

```yaml
name: STAC integrity
on: [push, pull_request]
jobs:
  semantic-integrity:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      # Until the package is on PyPI, install from a wheel or a source checkout instead.
      - run: python -m pip install stac-integrity-gate
      - run: stac-integrity collection stac/collection.json --limit 500
```

Add `--fail-unreadable` if assets that cannot be opened should also fail the job. Add `--strict` to fail on any warning.

## Checks

| code | severity | meaning |
|---|---|---|
| `CRS_MISMATCH` | ERROR | declared CRS is not equivalent to the raster CRS |
| `CRS_VERTICAL_UNVERIFIED` | WARN | declared compound CRS (e.g. EPSG:5498 = NAD83 + NAVD88 height) whose horizontal part equals the 2D raster CRS; the vertical part cannot be checked from the header |
| `CRS_UNPARSEABLE` | WARN | declared CRS could not be parsed (e.g. unknown or non-integral EPSG code), so it was not compared. An integral float such as `4326.0` is accepted as `4326` |
| `RASTER_CRS_MISSING` | ERROR | STAC declares a CRS but the raster has none |
| `SHAPE_MISMATCH` / `SHAPE_INVALID` | ERROR | `proj:shape` ≠ `[height, width]`, or malformed |
| `TRANSFORM_MISMATCH` / `TRANSFORM_INVALID` | ERROR | `proj:transform` differs beyond tolerance, or malformed (6 values, or 9 with a `[0,0,1]` tail) |
| `BBOX_MISMATCH` / `BBOX_INVALID` | ERROR | `proj:bbox` differs from raster bounds beyond tolerance, or malformed |
| `BAND_COUNT_MISMATCH` | ERROR | number of declared bands ≠ raster band count |
| `DATA_TYPE_MISMATCH` | ERROR | declared band `data_type` ≠ raster dtype |
| `NODATA_MISMATCH` | ERROR | the header has a nodata value and it differs from the declared `nodata` (NaN-aware), or STAC declares `nodata: null` while the header defines one |
| `NODATA_NOT_IN_HEADER` | WARN | STAC declares `nodata` but the header has no nodata tag. This does not prove the declaration wrong, because the fill value may still be used in the pixels |
| `SCALE_MISMATCH` / `OFFSET_MISMATCH` | WARN | declared scale/offset ≠ raster header scale/offset |
| `DUPLICATE_DATA_HREF` | WARN | several data assets point to the same href |
| `ASSET_UNREADABLE` | WARN (access failure) / ERROR (missing local file in a local catalog) | the asset could not be opened. It is ERROR only when a local catalog references a local file that cannot be opened. Remote hrefs, and `file://` or local-path hrefs inside a *remote* catalog (e.g. HPC-internal paths), are access failures and give WARN. Use `--fail-unreadable` to make every unreadable asset an ERROR |
| `NO_RASTER_ASSETS` | WARN | nothing was inspected: no selected data-role GeoTIFF/JPEG2000 assets |

Spatial comparisons use a default origin/bounds tolerance of `0.01` pixel (`--tolerance-px`).

## Severity model

- **ERROR** means the asset was actually read, and its header demonstrably contradicts the STAC declaration. Only ERRORs make the default run fail. (The one non-header ERROR is a local catalog pointing at a local file that does not open: in your own repository, that is a publishing error.)
- **WARN** is used whenever the evidence is insufficient to prove a mismatch: unreadable or access-restricted assets, a nodata value the header does not carry, vertical CRS components, scale/offset (STAC may describe semantic scaling not stored in the file), unparseable declarations and duplicate hrefs. Insufficient evidence is never an ERROR.
- **Access failures are never semantic failures.** An asset that returns 403, times out, needs credentials, or lives on the publisher's private filesystem (a `file://` href in a remote catalog) is reported as `ASSET_UNREADABLE` (WARN). Note that a run where nothing could be opened exits `0` with a `NO_RASTER_ASSETS` or `ASSET_UNREADABLE` warning. Use `--fail-unreadable` or `--strict` if that should fail CI.
- By default only assets with role `data` (or no roles) are audited. Use `--all-raster-assets` to include visual/overview assets.
- `Collection.item_assets` is **not** inherited into Items. STAC requires those fields to be repeated on the Item asset.

## Supported formats

| Input | Status |
|---|---|
| STAC Items (local file or HTTP(S) URL) | supported |
| STAC Collections: static `rel=item` links, or a STAC API `rel=items` endpoint with `next` paging | supported |
| STAC 1.1 Collection-level assets | supported |
| GeoTIFF / COG assets | supported and live-validated |
| JPEG2000 assets | opened when your GDAL build supports it; not live-validated |
| Local paths and `http(s)://` hrefs, including pre-signed URLs such as Azure SAS | supported and live-validated |
| `file://` hrefs | opened locally. In a remote catalog they usually point at the publisher's own filesystem; an open failure there gives `ASSET_UNREADABLE` WARN |
| `s3://` hrefs | passed to GDAL; live-validated for public buckets with `AWS_NO_SIGN_REQUEST=YES` |
| Other object-store URIs (`gs://`, `az://`, …) | passed to GDAL; not live-validated. Access failures are treated as remote (WARN) |

Credentials are never handled by the tool. Configure them the way GDAL expects (environment variables, or pre-signed hrefs), or pass an href resolver that returns the URL to open:

```bash
pip install "stac-integrity-gate[planetary-computer]"
stac-integrity item pc-item.json --resolver planetary-computer      # SAS-sign Planetary Computer blobs
GDAL_HTTP_BEARER="$TOKEN" stac-integrity item cdse-item.json --resolver alternate-https
stac-integrity item item.json --resolver mypackage.auth:resolve     # your own (href, context) -> str
```

Resolver failures are `ASSET_RESOLVE_FAILED` (WARN unless `--fail-unreadable`). Signatures and tokens are redacted from findings and error messages; `declared` always shows the original STAC href. Design and provider notes: [docs/auth-design.md](docs/auth-design.md).

## Known limitations

- **Zarr**: no semantic validation of Zarr stores.
- **GeoParquet**: no semantic validation of GeoParquet assets.
- **Authenticated catalogs**: only Planetary Computer signing is built in (optional extra). Other providers (CDSE, NASA Earthdata, USGS EROS, requester-pays S3) need GDAL configuration or your own resolver; without it their assets are reported as unreadable (WARN).
- **Vertical CRS**: the vertical component of a compound CRS cannot be verified from a 2D raster header (`CRS_VERTICAL_UNVERIFIED`).
- **Not every STAC extension** is covered, only the projection fields, bands/raster-band fields and duplicate hrefs listed above.
- **Not compared**: Item `geometry`, `bbox` and `gsd` are deliberately not treated as exact raster invariants. Pixel values and scientific correctness are not checked.
- A Collection audit covers the **first N Items** returned (`--limit`), not a random sample.
- Catalogs whose assets live on a private filesystem (e.g. HPC-internal `file://` paths), or behind authentication or bot protection, cannot be audited from outside. They produce warnings only.
- Only standard `proj:*`, `bands` and `raster:bands` fields are compared. A catalog that describes its rasters with custom fields has nothing to compare, and passes with no findings.

## Validation evidence

The tool was run against live public catalogs. Each finding was cross-checked with `rio info` and with an independent TIFF header parser that does not use GDAL. Summary and methodology: [docs/validation-evidence.md](docs/validation-evidence.md). Background on why the tool exists: [EVIDENCE.md](EVIDENCE.md).

## Development

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
.venv/bin/pytest -q
```

The test suite is fully offline. Live benchmark tooling lives in [`benchmark/`](benchmark/README.md) and is not part of the installed package. See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT. See [LICENSE](LICENSE).
