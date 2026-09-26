# Benchmark and live-validation tooling

Nothing in this directory is part of the installed package. The wheel contains only `stac_integrity/`, and `MANIFEST.in` prunes `benchmark/` from the sdist. The live scripts need outbound internet access, and some need extra packages that are not package dependencies (`planetary-computer`, `pystac`, `pystac-client`, `stac_valid`). The regression suite below needs neither.

## Regression suite (offline by default)

The failure families found in the Wave 1–3 live validations are registered in
[`manifest.json`](manifest.json) and reproduced offline by
[`fixtures/builders.py`](fixtures/builders.py), which writes a minimal GeoTIFF
(a few pixels) plus STAC JSON per case at run time. No binaries are committed
except the existing `demo_bad/` and `demo_collection_bad/` fixtures.

```bash
python -m pip install -e ".[test]"
python -m benchmark.run                                           # offline cases
python -m benchmark.run --baseline benchmark/baseline/offline.json  # + regression diff
python -m benchmark.run --case dea-nidem-crs-nodata               # one case (repeatable)
```

Output goes to `benchmark/results/regression/results.json` and `results.md`
(git-ignored; `--out-dir` to change). Exit status is 0 when every case meets its
expectation and the baseline diff has no regression, 1 otherwise, 2 on manifest
or usage errors.

Offline cases run the real CLI (`stac-integrity item|collection ... --json`) in
process. Any HTTP(S) fetch that the fixture did not provide fails the case, so
the offline suite cannot silently reach the network. A remote catalog (the DLR
terrabyte case) is simulated by serving its JSON for a fake URL.

The same comparison runs in `pytest` (`tests/test_benchmark_regressions.py`,
well under a second), so CI needs no extra step.

### Live mode (explicit opt-in)

```bash
python -m benchmark.run --live                   # live cases only
python -m benchmark.run --mode all               # offline + live
python -m benchmark.run --live --http-stats      # + approximate HTTP counts
```

Live cases either reference a `targets.json` entry by name (`"live": {"target": "..."}`)
or define one inline (`kind`, `source`, `assets`, `limit`); they run through
`scripts/public_benchmark.py:run_target`, so `AWS_NO_SIGN_REQUEST=YES` is the
default. The manifest `expected.signal` overrides the target's `expected_signal`.
Live findings match as a subset (real catalogs may carry extra WARNs), but a
control may never emit an unexpected ERROR. `INCONCLUSIVE` from auth/WAF/network
is an operational result, not a detector regression.

`--http-stats` turns on GDAL `CPL_DEBUG` and counts `GetFileSize` probes (and
how many were answered 206 with data), explicit ranged GETs with their byte
span, and STAC JSON fetches. The numbers are indicative only: GDAL's cache
hides repeated reads in one process, the message format is GDAL-version
dependent, and bytes are a lower bound.

### Manifest schema (`schema_version: 1`)

| field | meaning |
|---|---|
| `id` | unique case id; baseline diffs are keyed by it |
| `family` | failure family (e.g. `shape-off-by-one`, `clean-control`) |
| `role` | `known-bad` (expected ERRORs) or `control` (must never ERROR) |
| `kind` | `offline-fixture` or `live` |
| `source` | provenance: `publisher`, `collection`, `asset`, `wave`, `reference` (issue URL or evidence section), `observed` |
| `fixture.builder` | offline: name registered in `fixtures/builders.py` |
| `live` | live: `{"target": "<targets.json name>"}` or an inline target |
| `expected.findings` | list of `{code, severity}`; offline = exact set of (code, severity), live = subset (`"match"` overrides) |
| `expected.exit_code` | offline: CLI exit code |
| `expected.signal` | live: `MISMATCH_FOUND` / `NO_MISMATCH_FOUND` / `INCONCLUSIVE` |
| `notes` | free text |

### Baseline diff semantics

`--baseline <results.json>` compares case by case (by `id`):

| bucket | counts as regression |
|---|---|
| new false positives: an ERROR the manifest does not expect (on a control, any ERROR) that the baseline did not have | yes |
| new false negatives: an expected ERROR missing now but present in the baseline | yes |
| changed severities: a code present in both runs with a different severity set | yes |
| changed exit codes / live signals | yes |
| newly failing (PASS → FAIL/ERROR) | yes |
| cases removed (ignored with `--case` or a different mode) | yes |
| timing: audit time > baseline × `--timing-factor` (2.0) **and** +`--timing-min-seconds` (0.25) | only with `--fail-on-timing` |
| resolved FPs / FNs, newly passing, cases added | no (improvements / info) |

Results store only `code`, `severity`, `asset`, `field` per finding (no
messages, hrefs or local paths), so baselines are portable.

### Adding a case

1. Add a builder to `fixtures/builders.py` with `@fixture("my_case")`. Keep the
   raster tiny, keep the ratios/coefficients that trigger the finding, and
   anonymize ids/paths. Return `FixtureSpec("item"|"collection", path)`.
2. Add a manifest entry with provenance and the expected findings + exit code.
3. `python -m benchmark.run --case my-case`, then update the baseline (below).
4. Optionally add the live counterpart (`kind: live`).

### Updating the baseline

Only after an intended behaviour change, reviewed in the diff:

```bash
python -m benchmark.run --baseline benchmark/baseline/offline.json   # review the diff first
python -m benchmark.run --write-baseline benchmark/baseline/offline.json
```

Commit the new `benchmark/baseline/offline.json` with the change that caused it.

## Public benchmark (cross-provider smoke test)

```bash
python -m pip install -e .
python scripts/public_benchmark.py            # targets: benchmark/targets.json
```

Writes `benchmark/results/report.json` and `report.md`. The harness sets `AWS_NO_SIGN_REQUEST=YES` unless you set it yourself, so public S3 buckets are read unsigned. Assets needing credentials remain INCONCLUSIVE.

This is a cross-provider smoke test, not a prevalence estimate. For a prevalence estimate, use randomized/stratified STAC API searches rather than the first N Items.

## Independent verification helpers

| script | purpose |
|---|---|
| `verify_tiff_header.py` | GeoTIFF header reader that uses **no GDAL/rasterio**: HTTP range requests plus a direct IFD/GeoKey parse. Used to cross-check tool findings |
| `independent_verify.py` | Wave 1: STAC declarations vs `verify_tiff_header.py` for the targeted benchmark outputs |
| `stratified_sample.py` | Wave 1: time-stratified Earth Search sample (tool + independent reader) |
| `wave2_pc.py` | Wave 2: Planetary Computer `3dep-seamless` sample. Signs hrefs, runs the tool, `rio info` (`RIO_BIN` env var, or `rio` on `PATH`) and the independent reader |
| `wave2_analyze.py` | Wave 2: per-item agreement between the tool and the independent readers |
| `wave2_nasa.py`, `wave2_nasa_titiler.py` | Wave 2: NASA GHG Center / VEDA controls, and the publisher-titiler supplementary comparison |

Raw results go to `benchmark/results/` (git-ignored). A summary is in [`docs/validation-evidence.md`](../docs/validation-evidence.md).
