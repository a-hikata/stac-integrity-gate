# Release Candidate Report — stac-integrity-gate 0.3.0rc1

- Date: 2026-09-26 (UTC 08:33–08:45)
- Working copy: local repository only. **No remote is configured.** Nothing was pushed, no remote repository or tag was created, and nothing was uploaded to PyPI.
- Base: commit `6e0cb5d` ("import v0.2 as delivered"), plus the uncommitted Wave 1 / Wave 2 patches, which are preserved. The pre-work state is recorded in `release/pre/`, which is git-ignored.

## Version

- `0.3.0rc1`, single source of truth in `stac_integrity/__init__.py` (`__version__`). `pyproject.toml` reads it via `[tool.setuptools.dynamic]`.
- Consistent across the package metadata (`importlib.metadata`), `stac-integrity --version`, the wheel/sdist filenames and the HTTP `User-Agent` (`stac-integrity-gate/0.3.0rc1`).

## Tests

| stage | result |
|---|---|
| before any RC change | 25 passed |
| final, dev venv (Python 3.11.3, rasterio 1.4.4 / GDAL 3.10.3) | **51 passed** |
| final, from the unpacked sdist in a clean venv (Python 3.12, rasterio 1.5.1) | **51 passed** |

The suite is fully offline: remote, 403 and auth failures are simulated with `monkeypatch`. The 26 new tests cover:

- one known-good and one known-bad fixture per invariant, with explicit severity (CRS, shape, transform, bbox, band count, dtype, nodata, scale, offset)
- CRS matches in string, integer and integral-float forms
- non-integral or unknown EPSG values giving WARN, not ERROR
- an auth-required asset giving WARN
- a known-good Collection-level asset
- the CLI exit-code contract (0 / 1 / 1 with `--strict` / 2), `--json`, the legacy invocation, `--version`, and the bundled demo fixtures

## Build

- Clean build venv (Homebrew Python 3.12, `build`, `twine`): `python -m build` produced a wheel and an sdist. `twine check`: **PASSED** for both.
- Wheel contents: `stac_integrity/*.py` plus dist-info (including LICENSE) only.
- sdist contents: the package, tests, demo fixtures, `scripts/public_benchmark.py` (needed by tests), README / CHANGELOG / CONTRIBUTING / EVIDENCE / docs, and LICENSE. `benchmark/`, `research/`, `release/` and all raw results are pruned via `MANIFEST.in`.
- SHA-256:
  - `stac_integrity_gate-0.3.0rc1-py3-none-any.whl` `7ff44a26b5865e9e80800dbf3873cae6e229832eec8e96fc216dc1fd3b9a172f`
  - `stac_integrity_gate-0.3.0rc1.tar.gz` `1025386713e48b5f750b43b3c1978ca43569ca2bf7de10fb9df4e5394b5c6473`

## Install

- Wheel installed into a fresh venv. It pulls only `rasterio` and its own dependencies (affine, attrs, certifi, click, numpy, pyparsing), with no dev or test dependencies.
- The sdist also installed into a fresh venv with `.[test]`, and its tests passed.

## Smoke test (installed wheel, clean venv)

| command | result |
|---|---|
| `stac-integrity --version` | `stac-integrity 0.3.0rc1` |
| `stac-integrity item demo_bad/item.json` | FAIL, BAND_COUNT_MISMATCH, exit 1 |
| `stac-integrity collection demo_collection_bad/collection.json` | FAIL, `findings: BAND_COUNT_MISMATCH=1`, exit 1 |
| `stac-integrity demo_bad/item.json` (legacy) | exit 1 |
| `... --asset data --json`, `... --limit 20 --json --summary-only` | valid JSON |
| `python -m stac_integrity item demo_bad/item.json` | exit 1 |
| missing input file | exit 2 with an `stac-integrity:` error |
| `collection` given an Item | exit 2 (`Source is not a STAC Collection`) |

## README command examples

- The Quick start and Example failure commands were executed. Their outputs in the README are verbatim copies of real output.
- The exit-code table matches `cli.py` and is covered by `tests/test_cli.py`.
- The CI example relies on exit `1` for ERROR, which was verified. The `pip install stac-integrity-gate` step is annotated as working only after PyPI publication.
- The `https://example.org/...` examples are placeholders and cannot be run.

## Secret scan

- Patterns searched: Azure SAS (`sig=`, `se=`, `skoid=`), AWS keys (AKIA/ASIA, secret, session token), bearer tokens, `Authorization:`, password, API keys, `token=`, private keys, `/Users/`, the local username, and personal e-mail.
- Scope: every file to be committed, the built sdist contents and all git history. **No hits.** See `release/secret-scan-final.txt`.
- One fix was made: a hard-coded personal path (`/Users/.../anaconda3/bin/rio`) in `benchmark/wave2_pc.py` was replaced with the `RIO_BIN` env var or `rio` from `PATH`.
- The git-ignored local `benchmark/results/` contains the local path and username inside GDAL error messages, but no SAS tokens or credentials. These files are neither committed nor packaged.
- The git author e-mail is a personal address. See RELEASE_BLOCKERS.md.
- PyPI name check: `stac-integrity-gate`, `stac-integrity`, `stac_integrity` and `stac-gate` are all unregistered (HTTP 404).

## Files added / changed (vs `6e0cb5d`)

- **Package:**
  - `stac_integrity/audit.py`: remote-URI handling (Wave 1), compound CRS and float EPSG (Wave 2), `USER_AGENT`
  - `stac_integrity/cli.py`: `--version`
  - `stac_integrity/collection.py`: `USER_AGENT`
  - `stac_integrity/__init__.py`: version
- **Packaging:**
  - `pyproject.toml`: dynamic version, `setuptools>=77` (required for the SPDX `license` string), `license-files`, classifiers, keywords, `test` extra
  - `MANIFEST.in` (new)
  - `.gitignore`: added `release/`
- **Tests:**
  - `tests/test_audit.py` (Wave 1/2 regressions)
  - new: `tests/test_public_benchmark.py`, `tests/test_invariants.py`, `tests/test_cli.py`
- **Docs:**
  - rewritten `README.md`
  - new: `docs/validation-evidence.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `RELEASE_BLOCKERS.md`, `RELEASE_CANDIDATE_REPORT.md`
  - `benchmark/README.md`
  - `examples/github-actions.yml`: PyPI note
- **Benchmark tooling, not packaged:** `benchmark/verify_tiff_header.py`, `independent_verify.py`, `stratified_sample.py`, `wave2_pc.py`, `wave2_analyze.py`, `wave2_nasa.py`, `wave2_nasa_titiler.py`, and `scripts/public_benchmark.py` (unsigned-access default)
- **Moved, not deleted:** `CLAUDE_CODE_LIVE_VALIDATION.md` and `CLAUDE_CODE_PROMPT.md` → `research/handoff/`

The public Python API (`audit_item`, `audit_collection`, `AuditResult`, `CollectionAuditResult`, `Finding`) is unchanged. Only one new public constant was added: `stac_integrity.audit.USER_AGENT`.

## Blockers

1. The commit author identity (a personal e-mail address) must be decided before the first public push. See `RELEASE_BLOCKERS.md`.

## Remaining limitations

- No Zarr or GeoParquet semantic validation.
- No built-in auth or signing: assets needing credentials give WARN / INCONCLUSIVE.
- Vertical CRS cannot be verified from 2D headers.
- JPEG2000 is not live-validated.
- Collection audits cover the first N Items.
- Validation evidence is a sample, not a prevalence estimate.

## Public release readiness

# NOT READY

The code, tests, build, install and docs are release-quality, and one BLOCKER remains open: the git author identity decision. Once it is resolved — keep the address, or rewrite the unpushed history to a public identity — no other blocker remains.
