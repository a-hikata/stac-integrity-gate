# Releasing

Releases are published to PyPI by `.github/workflows/release.yml` when a
version tag is pushed. Before the first release, work through
[release-checklist.md](release-checklist.md).

## How the release workflow works

Trigger: `push` of a tag matching `v*` (for example `v0.3.0rc1`). Branch pushes
and pull requests never publish.

| Job | Permissions | What it does |
|---|---|---|
| `test` | `contents: read` | Full offline test suite on Python 3.10–3.13. |
| `build` | `contents: read` | Fails unless the tag equals `v` + `stac_integrity.__version__`. Builds wheel + sdist, runs `twine check --strict`, installs the wheel into a clean venv and runs `stac-integrity --version`. Uploads `dist/` as a workflow artifact. |
| `publish` | `id-token: write` only | Runs in the GitHub environment `pypi`. Downloads the artifact built above (it does not rebuild) and uploads it with `pypa/gh-action-pypi-publish`, which also uploads PEP 740 attestations. |

## Why this is safe to keep in the repository

- **No stored credentials.** Upload uses [PyPI Trusted Publishing](https://docs.pypi.org/trusted-publishers/)
  (OIDC). There is no API token in the repository or in GitHub secrets.
- **Fails closed.** Until a Trusted Publisher is registered on PyPI for this
  repository + `release.yml` + environment `pypi`, the upload step is rejected
  by PyPI. Having the file in the repository publishes nothing by itself.
- **Least privilege.** The workflow default is `contents: read`. Only the
  `publish` job can mint an OIDC token, and it runs no project code: it only
  downloads the already-tested artifact and uploads it.
- **Human gate.** Configure the `pypi` environment with *required reviewers*
  and a deployment rule that allows only tags `v*`. Each upload then needs a
  manual approval in the GitHub UI.
- **Version consistency.** A tag that does not match the package version
  fails before anything is built for upload.

A PyPI upload cannot be undone (a version number can never be reused, even
after deletion), so the manual environment approval is strongly recommended.

## One-time setup (after the public repository exists)

1. GitHub: *Settings → Environments → New environment* `pypi`.
   - Required reviewers: the maintainer(s).
   - Deployment branches and tags: *Selected*, add tag rule `v*`.
2. PyPI: register a *pending* Trusted Publisher for project
   `stac-integrity-gate` (Account → Publishing):
   owner / repository = the public repository, workflow = `release.yml`,
   environment = `pypi`.
3. Optional dry run: create a TestPyPI pending publisher and temporarily point
   a copy of the publish job at TestPyPI
   (`repository-url: https://test.pypi.org/legacy/`, environment `testpypi`).

## Cutting a release

1. Update `stac_integrity/__init__.py` (`__version__`) and `CHANGELOG.md`.
2. Merge to `main` and wait for CI to pass.
3. Tag the merge commit and push the tag:
   `git tag -a v<version> -m "stac-integrity-gate <version>"` and
   `git push origin v<version>`.
4. Approve the `publish` job in the `pypi` environment.
5. Verify the project page and install from PyPI in a clean venv.

## Manual fallback

If Trusted Publishing is not available, build and check locally
(`python -m build`, `python -m twine check --strict dist/*`) and upload with
`twine upload` using a project-scoped API token entered interactively. Never
commit the token or store it in a file inside the repository.
