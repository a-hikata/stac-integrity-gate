# Release checklist

Pre-publish checklist for the first public push and PyPI upload of
`stac-integrity-gate`. Work top to bottom; every step is required unless marked
optional. See [releasing.md](releasing.md) for how the release workflow works.

## 1. Identity and history (blocker)

- [ ] Choose the public author identity (name + e-mail). A GitHub no-reply
      address (`<id>+<user>@users.noreply.github.com`) is recommended.
- [ ] Rewrite the unpushed history with the prepared script in the maintainer's
      local checkout (git-ignored, not part of the repository):
      `release/rewrite-author-identity.sh "<Public Name>" "<public-email>"`.
      Run its dry-run mode first and read the output.
- [ ] Verify: `git log --all --format='%an <%ae> | %cn <%ce>' | sort -u` shows
      only the public identity.
- [ ] Set the identity for future commits in this repository:
      `git config user.name` / `git config user.email`.
- [ ] Confirm `LICENSE` copyright holder is the intended one.

## 2. Content and secret scan

- [ ] No credentials, tokens, signed URLs (`sig=`, `X-Amz-Signature`, SAS
      tokens) or personal paths (`/Users/`, `/home/`) in tracked files:
      `git grep -nE 'sig=|X-Amz-|token|password|/Users/|/home/'` and review
      each hit.
- [ ] No personal e-mail address in tracked files or history content:
      `git grep -n '@'` and review.
- [ ] Optional: run a dedicated scanner (e.g. `gitleaks detect`) over the full
      history if one is available.
- [ ] `release/`, `benchmark/results/`, `.venv/`, `dist/`, `build/` are
      git-ignored and not tracked: `git ls-files release benchmark/results dist build`
      prints nothing.

## 3. Version and changelog

- [ ] `stac_integrity/__init__.py` `__version__` is the version to publish.
- [ ] `CHANGELOG.md` has a dated section for that version (replace
      "unreleased").
- [ ] README status line and install examples match the version.

## 4. Build and verify artifacts

- [ ] Clean build: `rm -rf dist build *.egg-info && python -m build`.
- [ ] `python -m twine check --strict dist/*` passes for wheel and sdist.
- [ ] Wheel contains only `stac_integrity/` plus metadata:
      `python -m zipfile -l dist/*.whl`.
- [ ] sdist contains no `benchmark/`, `research/` or `release/`:
      `tar tzf dist/*.tar.gz`.
- [ ] Scan the artifacts for personal data:
      `unzip -p dist/*.whl | grep -a -c '@'` and
      `tar xzf dist/*.tar.gz -O | grep -a -nE '/Users/|/home/|@gmail'`; review any hits.
- [ ] Clean wheel install, outside the checkout:
      `python -m venv /tmp/v && /tmp/v/bin/pip install dist/*.whl && (cd /tmp && /tmp/v/bin/stac-integrity --version)`.
- [ ] Offline tests pass from the unpacked sdist:
      `pip install "<sdist>[test]"` then `pytest -q` inside the unpacked directory.
- [ ] `pytest -q` passes in the checkout on every supported Python (CI matrix
      3.10–3.13 green).

## 5. Repository metadata (after the public repository exists)

- [ ] Add `[project.urls]` (Homepage / Source / Issues / Changelog) to
      `pyproject.toml` with the real repository URL, rebuild, re-run step 4.
- [ ] Relative links in `README.md` (e.g. `docs/validation-evidence.md`)
      do not resolve on PyPI; convert them to absolute repository URLs if the
      PyPI page should link to them.

## 6. Trusted Publishing setup

- [ ] GitHub environment `pypi` exists with required reviewers and a tag rule
      `v*`.
- [ ] PyPI pending Trusted Publisher registered for project
      `stac-integrity-gate`, workflow `release.yml`, environment `pypi`.
- [ ] No PyPI API token is stored in GitHub secrets.
- [ ] Optional: dry run against TestPyPI first.

## 7. Push, tag, publish

- [ ] Push `main` to the public repository; CI (`ci.yml`) is green.
- [ ] Create an annotated tag on the CI-green commit:
      `git tag -a v<version> -m "stac-integrity-gate <version>"`.
- [ ] Push the tag: `git push origin v<version>`.
- [ ] `release.yml`: `test` and `build` jobs green; approve `publish`.
- [ ] Verify on PyPI: version, README rendering, metadata, attestations.
- [ ] Clean install from PyPI: `pip install stac-integrity-gate==<version>`
      then `stac-integrity --version`.

## 8. After publishing

- [ ] Remove the "not yet on PyPI" comments from `README.md` and
      `examples/github-actions.yml`.
- [ ] Update `RELEASE_BLOCKERS.md` / `CHANGELOG.md` to record the release.
