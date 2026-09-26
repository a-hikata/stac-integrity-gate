# Claude Code live-validation handoff

Use this only on a machine with normal outbound internet access.

## Goal

Run `stac-integrity-gate` v0.2 against real public STAC catalogs, identify true semantic mismatches, and separate them from access/authentication noise. Do not broaden product scope unless live evidence supports it.

## Instructions

1. Work from the repository root.
2. Create an isolated Python environment and install the package editable.
3. Run the full test suite first. Stop if any local test fails.
4. Run `python scripts/public_benchmark.py`.
5. Inspect `benchmark/results/report.json` and `report.md`.
6. For each `MISMATCH_FOUND`, independently verify the declared STAC value and actual raster header using `gdalinfo`, `rio info`, or another direct raster-header read.
7. For each `INCONCLUSIVE`, classify the cause:
   - authentication/signing required
   - unsupported asset type (e.g. Zarr)
   - network/GDAL issue
   - no raster assets
8. Do **not** convert access failures into semantic failures.
9. Specifically run these targeted checks:

```bash
stac-integrity item \
  https://earth-search.aws.element84.com/v1/collections/sentinel-2-l2a/items/S2B_53MLM_20240709_0_L2A \
  --asset aot --json > benchmark/results/earth-search-legacy-aot-known-bad.json

stac-integrity collection \
  https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a \
  --limit 10 --asset aot --json > benchmark/results/earth-search-aot.json

stac-integrity collection \
  https://earth-search.aws.element84.com/v1/collections/sentinel-2-c1-l2a \
  --limit 10 --asset red --json > benchmark/results/earth-search-red-control.json

stac-integrity collection \
  https://earth-search.aws.element84.com/v1/collections/cop-dem-glo-30 \
  --limit 10 --asset data --json > benchmark/results/cop-dem-control.json

stac-integrity collection \
  https://s3-west.nrp-nautilus.io/public-rap/rap-pfg-cover/stac-collection.json \
  --limit 1 --json > benchmark/results/nrp-rap-pfg.json
```

10. The legacy Earth Search Item is a regression control from Element84 issue #48. If it no longer mismatches, record that as a historical fix rather than forcing a failure.
11. If current Earth Search Collection-1 AOT reports a mismatch, verify at least 3 separate Items before treating it as current catalog evidence.
12. If a control (`red`, `cop-dem`) fails, determine whether the tool is wrong before changing the catalog hypothesis.
13. Patch the tool only for demonstrated false positives. Add a regression test for every patch.
14. Re-run `pytest -q` after each patch.
15. Produce `benchmark/results/LIVE_FINDINGS.md` with a compact table:

| Provider | Collection | Asset | Items tested | Real mismatches | False positives | Inconclusive | Verdict |

16. End with one of:
   - `GO`: current independent catalogs show real semantic mismatches missed by structural validation.
   - `HOLD`: only one producer/failure family reproduced.
   - `KILL`: current catalogs are clean or equivalent maintained tooling already solves it.

## Important constraints

- `Collection.item_assets` is not inherited into Item assets.
- Do not hard-fail scale/offset differences unless the storage semantics prove the STAC declaration is wrong.
- Do not treat Item geometry or `gsd` as exact raster-header invariants.
- Do not add Zarr support merely because it is interesting; add it only if the live findings show it is the next highest-value gap.
