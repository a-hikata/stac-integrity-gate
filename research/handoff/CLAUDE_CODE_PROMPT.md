# Paste this into Claude Code

Read `CLAUDE_CODE_LIVE_VALIDATION.md` and execute it end-to-end from this repository root.

Do not redesign the product or broaden scope first. Your job is to validate `stac-integrity-gate` v0.2 against live public STAC catalogs using this machine's outbound internet access.

Required outputs:

1. Run `pytest -q` before any change.
2. Run `python scripts/public_benchmark.py`.
3. Independently verify every `MISMATCH_FOUND` using direct raster-header inspection (`gdalinfo`, `rio info`, or equivalent).
4. Treat access/auth/network failures as `INCONCLUSIVE`, never as semantic mismatches.
5. Treat failures on the clean controls as probable tool bugs until disproven.
6. Patch only demonstrated false positives/operational bugs; add a regression test for every patch.
7. Re-run the full test suite after changes.
8. Write `benchmark/results/LIVE_FINDINGS.md` with the exact table and GO/HOLD/KILL verdict defined in `CLAUDE_CODE_LIVE_VALIDATION.md`.
9. Preserve all raw JSON benchmark outputs under `benchmark/results/`.

Do not add Zarr, GeoParquet, authentication integrations, or new product features unless the live evidence proves one is required to complete this validation.
