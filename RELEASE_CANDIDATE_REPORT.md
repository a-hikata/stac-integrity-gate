# Release Candidate Report — stac-integrity-gate 0.3.0rc1

- Updated: 2026-09-26 (UTC 10:07), after the Wave 3 blind audit fixes. The first RC pass ran 08:33–08:45 UTC.
- Working copy: local repository only. **No remote is configured.** Nothing was pushed, no remote repository or tag was created, and nothing was uploaded to PyPI.
- History: `6e0cb5d` (import v0.2) → `ca2d6b4` (RC) → the final RC commit with this report. The pre-work state for each pass is in `release/pre/` and `release/final-pre/`; QA logs are in `release/final/`. All three are git-ignored.
- The Wave 3 audit workspace (`~/stac-integrity-gate-wave3-audit`) was not modified. Its saved raw Items were only read, for the live re-check.

## Version

- `0.3.0rc1`, single source of truth in `stac_integrity/__init__.py`.
- Consistent across the package metadata, `stac-integrity --version`, the wheel/sdist filenames and the HTTP `User-Agent`.

## Changes in this pass (Wave 3 blind-audit fixes)

| ID | Problem (found blind) | Fix | Regression tests |
|---|---|---|---|
| FP-1 | A remote catalog with unreadable `file://` or local-path hrefs (DLR terrabyte, HPC-internal paths) gave `ASSET_UNREADABLE` **ERROR**, exit 1 | Unreadable local/`file://` hrefs are an ERROR only when the STAC source itself is local. For a remote source they follow `unreadable_severity`: WARN by default, ERROR with `--fail-unreadable` | `test_remote_item_with_unreadable_file_href_is_access_warning`, `…_honors_fail_unreadable`, `…_exits_0`, `test_local_item_with_missing_local_asset_is_still_error[missing.tif / file://]` |
| FP-2 | STAC `nodata: -9999` with no header nodata tag (DEA `nidem`, where 99.4% of pixels are -9999) gave `NODATA_MISMATCH` **ERROR** | New WARN `NODATA_NOT_IN_HEADER`. A different header nodata is still `NODATA_MISMATCH` ERROR | `test_declared_nodata_vs_header[header-absent-warn / header-differs-error / header-equal-pass]`, `test_declared_null_nodata_vs_header_nodata_is_error` |

- Additional offline fixtures in `tests/test_live_regressions.py` keep every live finding reproducible:
  - Wave 3 true mismatches: the DEA-nidem-like CRS 4326 vs 3577 case, and the DEA-MSTP-like shape off-by-one
  - Wave 2 regressions: compound CRS `EPSG:5498` vs `4269`, the 3DEP-like `1e-05` pixel-size error, and float `proj:epsg 4326.0`
- **Live re-check**, reading saved Wave 3 raw Items with assets from public S3:
  - DEA nidem gives `CRS_MISMATCH` ERROR plus `NODATA_NOT_IN_HEADER` WARN
  - DEA MSTP gives `SHAPE_MISMATCH` ERROR plus `NODATA_NOT_IN_HEADER` WARN
  - The true mismatches are still ERRORs; only the insufficient-evidence cases were downgraded. See `release/final/live-recheck.txt`.
- **Docs aligned with the code:**
  - README: problem examples, the Checks table (`NODATA_NOT_IN_HEADER`, `NODATA_MISMATCH`, `ASSET_UNREADABLE` wording), the Severity model ("insufficient evidence is never an ERROR"), Supported formats (`file://`) and Known limitations
  - CHANGELOG
  - `docs/validation-evidence.md`: Wave 3 section with the two blind discoveries, the two fixed false positives, and the note that this is a validation sample, not a prevalence study
  - `RELEASE_BLOCKERS.md`

The comparison logic for CRS, shape, transform, bbox, band count and dtype, and the tolerances, are unchanged.

## Tests

| stage | result |
|---|---|
| start of this pass | 51 passed |
| new regression tests before the fix | 3 failed (exactly FP-1 ×2 and FP-2 ×1), 11 passed |
| final, dev venv (Python 3.11.3, rasterio 1.4.4 / GDAL 3.10.3) | **65 passed** |
| final, from the unpacked sdist in a clean venv (Python 3.12, rasterio 1.5.1) | **65 passed** |

The suite is fully offline.

## Build

- Clean build venv (Python 3.12, `build`, `twine`): wheel and sdist were built. `twine check`: **PASSED** for both.
- Wheel: `stac_integrity/*.py` plus dist-info (LICENSE, METADATA, entry point) only.
- sdist: package, tests (including `test_live_regressions.py`), demo fixtures, `scripts/public_benchmark.py`, README / CHANGELOG / CONTRIBUTING / EVIDENCE / docs and LICENSE. `benchmark/`, `research/`, `release/` and raw results are excluded.
- SHA-256:
  - `stac_integrity_gate-0.3.0rc1-py3-none-any.whl` `713dc33d4ddc14e092e75a8eb53dc97414bbd8888a359d9d17f635f069ea2e2e`
  - `stac_integrity_gate-0.3.0rc1.tar.gz` `7500676600fe925378e2d1b2c268c03cf41110eb5368de28e29cb39a00025690`

These hashes change if the history or identity is rewritten and dist is rebuilt. Rebuild before publishing.

## Install

- Wheel into a fresh venv: OK. Only rasterio and its dependencies are installed (affine, attrs, certifi, click, numpy, pyparsing), with no dev or test dependencies.
- sdist into a fresh venv with `.[test]`: OK, and 65 tests passed.

## Smoke test (installed wheel, clean venv) and README command examples

| command | result |
|---|---|
| `stac-integrity --version` | `stac-integrity 0.3.0rc1` |
| `stac-integrity item demo_bad/item.json` | output identical to the README; exit 1 |
| `stac-integrity collection demo_collection_bad/collection.json` | output identical to the README; exit 1 |
| legacy `stac-integrity demo_bad/item.json` | exit 1 |
| `--asset data --json`, `--limit 20 --json --summary-only` | valid JSON |
| `python -m stac_integrity item …` | exit 1 |
| missing input | exit 2 |
| `collection` given an Item | exit 2 |
| remote Item with an unreadable `file://` asset | `[WARN] ASSET_UNREADABLE`, exit 0 (`tests/test_cli`-style test in `test_live_regressions.py`) |

The README CI example relies on exit 1 for ERROR, and exit 0 for warnings unless `--strict` is set. Both are verified and covered by tests.

## Secret scan

Written to `release/final/secret-scan.txt`.

- Patterns: Azure SAS, AWS keys and tokens, bearer tokens, `Authorization:`, password, API keys, `token=`, private keys, local home paths, the local username, and personal e-mail.
- **Wheel: no hits. sdist: no hits.**
- The only content hits are two lines of this report describing the scan (pattern names and an elided path), both in committed history. They are not secrets.
- **Commit metadata:** both existing commits carry the personal e-mail address as author and committer, and so will the commit adding this report. This is the open BLOCKER.

## Git author identity

| commit | author name | author email | committer name | committer email |
|---|---|---|---|---|
| `ca2d6b4` | Takahito Suzuki | <personal address, withheld> | Takahito Suzuki | <personal address, withheld> |
| `6e0cb5d` | Takahito Suzuki | <personal address, withheld> | Takahito Suzuki | <personal address, withheld> |
| (this RC commit) | Takahito Suzuki | <personal address, withheld> | Takahito Suzuki | <personal address, withheld> |

- The rewrite command is prepared: `release/rewrite-author-identity.sh`, git-ignored.
- It was dry-run tested on a throwaway clone. All commits were rewritten and the old address was verified gone.
- **It was not executed on this repository**, because no public e-mail address has been specified.

## Files added / changed in this pass

- `stac_integrity/audit.py`: the FP-1 and FP-2 fixes.
- `tests/test_live_regressions.py`: new, 14 tests.
- `README.md`, `CHANGELOG.md`, `docs/validation-evidence.md`, `RELEASE_BLOCKERS.md`, `RELEASE_CANDIDATE_REPORT.md`.

For the full list of files added and changed in the first RC pass, see commit `ca2d6b4`.

## Blockers

- **OPEN (1):** git author identity.
- **RESOLVED (2):** FP-1 (remote `file://` access-failure severity) and FP-2 (missing header nodata severity).
- See `RELEASE_BLOCKERS.md`.

## Remaining limitations

- No Zarr or GeoParquet semantic validation.
- No built-in auth or signing.
- Vertical CRS cannot be verified from 2D headers.
- JPEG2000 is not live-validated.
- Collection audits cover the first N Items.
- Catalogs using only custom raster fields give nothing to compare.
- The validation evidence is a sample, not a prevalence estimate.

## Public release readiness

# READY PENDING AUTHOR IDENTITY

Every blocker except the git author identity is resolved. The code, tests (65 passing, offline), build, install, docs and secret scan are release-ready. Once a public name and e-mail are provided, run `release/rewrite-author-identity.sh "<Public Name>" "<public-email>"`, rebuild `dist/`, and the RC is READY. Nothing has been pushed, tagged or uploaded.
