# Output formats

`stac-integrity item|collection` can emit four report formats:

```bash
stac-integrity item item.json                       # text (default, unchanged)
stac-integrity item item.json --format json         # same as --json
stac-integrity item item.json --format sarif        # SARIF 2.1.0 (GitHub code scanning)
stac-integrity collection c.json --format junit     # JUnit XML (GitLab / Jenkins test reports)
stac-integrity collection c.json --format sarif --output stac.sarif
```

## Invariants for every format

- **Exit codes are identical for every format**: `0` clean, `1` any ERROR (or any
  WARN with `--strict`), `2` operational failure (input could not be loaded, or the
  `--output` file could not be written).
- **Severity never changes with the format.** ERROR means the raster header
  contradicts the STAC declaration; WARN means insufficient evidence or access.
  `--strict` changes the *gate verdict* (exit code, JSON `status`, JUnit
  failures), never a finding's severity or SARIF level.
- On an operational failure (exit `2`) no report is written; the message goes to
  stderr. In CI, guard an upload step with `if: always() && hashFiles('stac.sarif') != ''`.

## `--output PATH`

Writes the selected `--format` report to `PATH` (parent directories are created)
and prints the normal **text** output on stdout, so the CI log stays readable while
the machine-readable file goes to an uploader. Without `--output` the report goes to
stdout. `--format text --output PATH` writes the text report to the file and also
prints it.

`--json` is kept as an alias of `--format json`; if both are given, the last one wins.

## JSON (`--format json` / `--json`)

The JSON report is `AuditResult.to_dict()` / `CollectionAuditResult.to_dict()` —
every historical key is kept with the same name and meaning — plus these
**additive** top-level keys:

| key | type | meaning |
|---|---|---|
| `schema_version` | string | report contract version, currently `"1.0"`. Minor bump = additive change; major bump = breaking change |
| `status` | `"pass"` \| `"fail"` | gate verdict; `"fail"` iff the exit code is `1` (so it honours `--strict`) |
| `strict` | bool | whether `--strict` was in effect |
| `summary.by_severity` | `{"ERROR": n, "WARN": n}` | counts over all findings (collection: collection assets + items) |
| `summary.by_code` | `{CODE: n}` | counts by finding code, sorted by code |

Note that `ok` keeps its historical meaning, "no ERROR findings", and ignores
`--strict`. Use `status` when you want the CI verdict.

Item report keys: `source, item_id, checked_assets, skipped_assets, ok, error_count,
warning_count, findings[]`. Each finding has `severity, code, asset, field, message,
declared, actual`.

Collection report keys: `source, collection_id, items_checked, checked_assets,
skipped_assets, ok, collection_assets_failed, failing_items, error_count,
warning_count, finding_codes`, plus optional `collection_assets` (an item report for
STAC 1.1 Collection-level assets) and `items[]` (item reports; omitted with
`--summary-only`). Nested item reports do not repeat the envelope keys.

A JSON Schema (draft 2020-12) for the report is in
[`report.schema.json`](report.schema.json). Consumers should ignore unknown keys.

## SARIF 2.1.0 (`--format sarif`)

- `version: "2.1.0"`, `$schema` points to the SARIF 2.1.0 JSON schema, one run.
- `runs[0].tool.driver`: `name: "stac-integrity-gate"`, `version`, and `rules[]`,
  one rule per finding code (`id` = code, `name` = CamelCase, `shortDescription`,
  `defaultConfiguration.level`). The full rule catalog is always emitted; a code
  that is not in the catalog still gets a generated rule.
- `results[]`: one per finding.
  - `ruleId` / `ruleIndex`, `level` (`ERROR` → `error`, `WARN` → `warning`), `message.text`
    (message plus item/asset/field).
  - `locations[0].physicalLocation.artifactLocation.uri`: the STAC JSON the finding
    came from. URLs are kept as-is; local paths under the current directory become
    repo-relative POSIX paths (what GitHub code scanning needs to annotate files);
    other local paths become `file://` URIs. For local files, `region.startLine`
    points at the asset key when it can be found.
  - `locations[0].logicalLocations[0].fullyQualifiedName`: `<item_id>/assets/<asset>/<field>`.
  - `partialFingerprints["stacIntegrityFinding/v1"]`: SHA-256 of item id, asset,
    field and code. It stays stable across runs, so alerts de-duplicate.
  - `properties`: `severity, item_id, asset, field, declared, actual`.
- `runs[0].properties`: `schema_version, status, strict, summary`, and the item or
  collection counts.

Structure only is tested offline (the official schema is not fetched).

## JUnit XML (`--format junit`)

```xml
<testsuites name="stac-integrity-gate" tests=".." failures=".." errors="0" skipped="..">
  <testsuite name="<item_id | collection_id>" ...>
    <properties> schema_version, tool_version, source, strict, status, counts </properties>
    <testcase classname="stac-integrity.item | stac-integrity.collection.<id>" name="<item_id>">
      <failure type="<first code>" message="N ERROR finding(s): CODES">finding lines</failure>
      <system-out>all finding lines (ERROR and WARN)</system-out>
    </testcase>
  </testsuite>
</testsuites>
```

- There is one `<testsuite>`: the item, or the collection. There is one `<testcase>`
  per STAC Item. A collection with Collection-level assets gets an extra testcase
  named `collection:<id>`. Testcases are per item, not per asset: the result model
  does not list assets that passed, so per-asset testcases would only list failures
  and the test count would change between runs.
- Mapping (mirrors the exit code):
  - any ERROR → `<failure>`
  - only WARNs, with `--strict` → `<failure>` (message says `WARN (--strict)`)
  - only WARNs, no asset inspected (`checked_assets == 0`, e.g. `ASSET_UNREADABLE`,
    `NO_RASTER_ASSETS`) → `<skipped>`. Nothing was verified either way.
  - otherwise the testcase passes. Warnings are listed in `<system-out>`.
- `errors="0"` always. Operational failures exit `2` and produce no report.
- `time="0"`, because per-item timing is not measured. GitLab
  (`artifacts:reports:junit`) and Jenkins (`junit` step) accept this layout.

## GitHub Actions

See [`examples/github-actions.yml`](../examples/github-actions.yml) for a gate step
plus SARIF upload through `github/codeql-action/upload-sarif`. There is no composite
action (`action.yml`). The CLI is a single command, so a composite action would only
wrap `pip install` + one command and add a versioned public surface to maintain.

## GitLab CI

```yaml
stac-integrity:
  image: python:3.12
  script:
    - pip install stac-integrity-gate
    - stac-integrity collection stac/collection.json --format junit --output stac-integrity.xml
  artifacts:
    when: always
    reports:
      junit: stac-integrity.xml
```
