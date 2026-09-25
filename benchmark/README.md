# Public benchmark

Run from the repository root on a machine with normal outbound internet access:

```bash
python -m pip install -e .
python scripts/public_benchmark.py
```

Outputs:

- `benchmark/results/report.json`
- `benchmark/results/report.md`

This is a cross-provider smoke test, not a prevalence estimate. For a prevalence estimate, use randomized/stratified STAC API searches rather than the first N Items.
