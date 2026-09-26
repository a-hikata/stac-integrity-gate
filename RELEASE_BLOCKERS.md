# Release blockers — stac-integrity-gate 0.3.0rc1

Reviewed: 2026-09-26. Only items that must be resolved **before the first public push/upload** are BLOCKERs. New features are never blockers.

## BLOCKER

1. **Commit author identity in git history.** Every local commit, including the RC commit, records the maintainer's personal e-mail address as author/committer. Once pushed to a public repository or included in an sdist built from a public repo, the address is public and effectively cannot be withdrawn.
   - **Decision required before the first push:** keep the address as is, or rewrite the local history to a public/no-reply identity. Rewriting is safe now because nothing has been pushed; afterwards it is not.
   - The current wheel/sdist themselves contain no e-mail address: `release/secret-scan-final.txt` records no hits.

LICENSE: present (MIT, `Copyright (c) 2026 stac-integrity-gate contributors`) and unchanged, so it is **not** a blocker.

## SHOULD FIX (can follow the first public release)

- Add `[project.urls]` (Homepage / Source / Issues) to `pyproject.toml` once the public repository exists. The PyPI page will otherwise have no links.
- The README and `examples/github-actions.yml` CI snippets use `pip install stac-integrity-gate`, which only works after the PyPI upload. Both carry a comment saying so; drop the comment after publishing.
- `.github/workflows/ci.yml` installs `pytest numpy` ad hoc; switch it to `pip install -e ".[test]"`.
- `EVIDENCE.md` predates the live validation (e.g. the NRP band-count case is now fixed upstream). Point it at, or merge it into, `docs/validation-evidence.md`.
- A run where nothing could be opened (`NO_RASTER_ASSETS` / `ASSET_UNREADABLE` only) exits `0`. This is documented, but consider a dedicated flag or exit status so that CI cannot silently "pass" an unreadable catalog.
- JPEG2000 assets are supported by code path but not live-validated (all JP2 targets tested required authentication).
- The rasterio `PendingDeprecationWarning` (affine `*` operator) shows in test output. It comes from rasterio itself, not this package.

## FUTURE (out of scope for this release)

- Zarr semantic validation (internal metadata, scale/offset duplication).
- GeoParquet semantic validation.
- Built-in authentication/URL signing for specific providers (Planetary Computer SAS, CDSE, NASA Earthdata/OIDC).
- Vertical CRS verification beyond the 2D header.
- Random or stratified sampling for `collection` audits (currently the first N Items).
- Checks for further extensions (e.g. `file:checksum`, `file:size`, `eo:bands` cross-checks).
- A prevalence study across public catalogs, as distinct from a validation sample.
