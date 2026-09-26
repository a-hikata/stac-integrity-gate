# Contributing

## Setup

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[test]"
```

## Running tests

```bash
.venv/bin/pytest -q
```

The test suite must stay **offline**: build fixtures with rasterio in `tmp_path`, and simulate remote or auth failures with `monkeypatch`. Live-catalog checks belong in `benchmark/`, not in `tests/`.

## Code style

- Follow the style of the surrounding code: standard library plus rasterio, type hints, small pure check functions that yield `Finding`s.
- Do not add runtime dependencies without a strong reason. The package deliberately depends only on rasterio.

## Adding or changing an invariant

1. **Add both fixtures.** Every new check needs a known-good case (a correct declaration that produces no finding) and a known-bad case (a wrong declaration that produces the expected code **and severity**). See `tests/test_invariants.py`.
2. **Choose the severity conservatively.** Use ERROR only when the raster header *proves* the declaration wrong. If the header cannot prove it (semantic scaling, vertical CRS, access problems), use WARN.
3. **False positives are worse than missed errors.** A gate that fails correct catalogs gets disabled. A new ERROR that might fire on valid data is not acceptable, even if it catches real bugs elsewhere. When in doubt, ship it as WARN first.
4. **Access failures are never semantic failures.** Unreadable, unauthorized or timed-out assets must not produce a mismatch ERROR.
5. Every bug fix driven by live data gets a regression test that names the source (see the existing `Live regression` tests).

## Before opening a pull request

- [ ] `pytest -q` passes.
- [ ] New or changed checks have known-good and known-bad fixtures with explicit severity assertions.
- [ ] README "Checks", "Severity model" and "Known limitations" are updated if behaviour changed.
- [ ] `CHANGELOG.md` has an entry.
- [ ] No credentials, signed URLs or personal paths in code, tests, docs or fixtures.
