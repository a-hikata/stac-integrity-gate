# Benchmark and live-validation tooling

Nothing in this directory is part of the installed package. The wheel contains only `stac_integrity/`. These scripts need outbound internet access, and some need extra packages that are not package dependencies (`planetary-computer`, `pystac`, `pystac-client`, `stac_valid`).

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
