# Release blockers — stac-integrity-gate 0.3.0rc1

Reviewed: 2026-09-26 (updated after the Wave 3 blind audit). Only items that must be resolved **before the first public push/upload** are BLOCKERs. New features are never blockers.

## BLOCKER

1. **Commit author identity in git history** — *OPEN, the only remaining blocker.* Every local commit, including the RC commits, records the maintainer's personal e-mail address as author and committer. Once it is pushed publicly, the address cannot effectively be withdrawn.
   - **Decision required before the first push:** keep the address, or rewrite the unpushed history to a public identity. A GitHub no-reply address is recommended.
   - A prepared, dry-run-tested command exists: `release/rewrite-author-identity.sh "<Public Name>" "<public-email>"` (local, git-ignored). It is **not executed**, because no public e-mail address has been specified.
   - The built wheel and sdist contain no e-mail address (`release/final/secret-scan.txt`).

LICENSE: present (MIT, `Copyright (c) 2026 stac-integrity-gate contributors`) and unchanged, so it is **not** a blocker.

## RESOLVED (were BLOCKERs, fixed in 0.3.0rc1 with regression tests)

1. **FP-1: remote `file://` access failure severity** — RESOLVED 2026-09-26.
   - Problem: a remote catalog whose asset hrefs are unreadable `file://` or local paths (found blind on DLR terrabyte, where they are HPC-internal paths) produced `ASSET_UNREADABLE` **ERROR** and exit 1. This contradicted "access failures are never semantic failures".
   - Fix: when the STAC source is remote, such failures use `unreadable_severity` (WARN by default, ERROR with `--fail-unreadable`). A local catalog that references a missing local file is still an ERROR.
   - Tests: `tests/test_live_regressions.py::test_remote_item_with_unreadable_file_href_*` and `::test_local_item_with_missing_local_asset_is_still_error`.
2. **FP-2: missing header nodata severity** — RESOLVED 2026-09-26.
   - Problem: STAC `nodata` with no nodata tag in the header produced `NODATA_MISMATCH` **ERROR**. The case was found blind on DEA `nidem`: STAC `-9999`, header none, and 99.4% of the pixels are `-9999`.
   - Fix: this case is now `NODATA_NOT_IN_HEADER` **WARN**. A header nodata that differs from the declaration is still `NODATA_MISMATCH` ERROR.
   - Tests: `tests/test_live_regressions.py::test_declared_nodata_vs_header[header-absent-warn|header-differs-error|header-equal-pass]` and `::test_declared_null_nodata_vs_header_nodata_is_error`.

## SHOULD FIX (can follow the first public release)

- Add `[project.urls]` (Homepage / Source / Issues) to `pyproject.toml` once the public repository exists. The PyPI page will otherwise have no links.
- The README and `examples/github-actions.yml` CI snippets use `pip install stac-integrity-gate`, which only works after the PyPI upload. Both carry a comment saying so; drop the comment after publishing.
- `.github/workflows/ci.yml` installs `pytest numpy` ad hoc; switch it to `pip install -e ".[test]"`.
- `EVIDENCE.md` predates the live validation (e.g. the NRP band-count case is now fixed upstream). Point it at, or merge it into, `docs/validation-evidence.md`.
- A run where nothing could be opened (`NO_RASTER_ASSETS` / `ASSET_UNREADABLE` only) exits `0`. This is documented, but consider a dedicated flag or exit status so that CI cannot silently "pass" an unreadable catalog.
- JPEG2000 assets are supported by code path but not live-validated (all JP2 targets tested required authentication).
- Coverage: catalogs that describe rasters only with custom fields (e.g. Brazil Data Cube's `bdc:raster_size`) or only `eo:bands` give nothing to compare. Consider an `eo:bands` band-count comparison, which would be a new invariant.
- The rasterio `PendingDeprecationWarning` (affine `*` operator) shows in test output. It comes from rasterio itself, not this package.

## FUTURE (out of scope for this release)

- Zarr semantic validation (internal metadata, scale/offset duplication).
- GeoParquet semantic validation.
- Built-in authentication/URL signing for specific providers (Planetary Computer SAS, CDSE, NASA Earthdata/OIDC).
- Vertical CRS verification beyond the 2D header.
- Random or stratified sampling for `collection` audits (currently the first N Items).
- Checks for further extensions (e.g. `file:checksum`, `file:size`, `eo:bands` cross-checks).
- A prevalence study across public catalogs, as distinct from a validation sample.
