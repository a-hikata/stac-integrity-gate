# Authenticated assets: design

Status: implemented in `feature/authenticated-assets` (Track B).

## Problem

Many real catalogs publish hrefs that cannot be opened as-is:

| Provider | What the href needs | Live evidence |
|---|---|---|
| Microsoft Planetary Computer | Azure SAS token appended to the blob URL | `planetary_computer.sign(href)` works; we signed manually in Wave 2 |
| Copernicus Data Space (CDSE) | `s3://eodata/...` needs S3 keys + custom endpoint; `alternate.https` needs an OIDC bearer token | both fail anonymously |
| NASA VEDA / GHG Center S3 | AWS credentials (Earthdata / temporary STS) | anonymous S3 fails |
| USGS LandsatLook | EROS login (HTTP redirect to login page) | anonymous fails |
| Earth Search NAIP | Requester-pays S3 (`AWS_REQUEST_PAYER=requester` + your own keys) | anonymous fails |

Today all of these become `ASSET_UNREADABLE` WARN: correct (access failure is not a
semantic error), but the header is never compared.

## Options

### (a) Authentication built into the core

`audit_item(..., provider="planetary-computer", credentials=...)`, with per-provider code
paths inside `audit.py` (SAS signing, S3 key handling, OIDC token exchange, EROS login).

- + One flag for users of the supported providers.
- − Every provider adds code, dependencies and credential-handling surface to the core.
  OIDC/EROS/STS flows change often and need network + secrets to test.
- − The tool starts to *store and transmit credentials*, which is a large security
  responsibility for a validation tool that is meant to run in CI.
- − Mixes two concerns: "how do I reach the bytes" (transport) and "does the header
  match the STAC declaration" (semantics). Bugs in the first would look like results of
  the second.
- − Core would grow optional imports / hard dependencies (`planetary-computer`,
  `boto3`, `requests-oauthlib`…), contradicting the rasterio-only core.

### (b) Resolver callback (recommended)

Separate transport/auth from semantic comparison with one hook:

```python
href_to_open = resolver(declared_href_resolved, context)
```

- The semantic core stays unchanged: it opens *whatever string the resolver returns*
  with rasterio and compares the header with the **declared** STAC fields.
- Default resolver = identity → existing behaviour is byte-for-byte unchanged.
- Provider specifics live in small optional resolvers (Planetary Computer ships as one,
  behind an extra); everything else is user code or GDAL configuration the user already
  owns (env vars, pre-signed URLs).
- Credentials never pass through the tool's API: a resolver *returns a URL*; GDAL
  reads its own env. The tool never serializes env or headers.
- Easy to test offline: resolvers are pure functions; stub them.

Recommendation: **(b)**.

## Interface

```python
from stac_integrity import HrefContext, audit_item

def my_resolver(href: str, context: HrefContext) -> str:
    # context.asset_key, context.item_id, context.source, context.asset (read-only)
    return href  # or a signed / alternate URL

audit_item("item.json", resolver=my_resolver)
audit_collection("collection.json", resolver=my_resolver)
```

- `HrefResolver = Callable[[str, HrefContext], str]` (also a `typing.Protocol`).
- The `href` argument is the declared href resolved against the Item location
  (relative → absolute), i.e. exactly what would be opened without a resolver.
- The resolver is called only for assets that will actually be opened (raster +
  data-role filter already applied), once per asset. It must be thread-safe
  (`audit_collection` runs Items in a thread pool).
- Raster detection (`.tif`, media type) and duplicate-href detection use the declared
  href, not the resolved one, so signing does not change which assets are checked.

### Failure semantics

| Situation | Finding | Severity |
|---|---|---|
| resolver raises, or returns a non-string / empty value | `ASSET_RESOLVE_FAILED` | `unreadable_severity` (WARN by default, ERROR only with `--fail-unreadable`) |
| resolved href cannot be opened (403, 404, timeout…) | `ASSET_UNREADABLE` | as before: WARN for remote, ERROR only for a missing local file in a local catalog |

A new code rather than reusing `ASSET_UNREADABLE`: a resolver failure means *the user's
signing/credential setup is broken* (e.g. package missing, token service down), while
`ASSET_UNREADABLE` means *the server refused or the file is absent*. They need different
fixes, and CI dashboards can count them separately. Neither is ever a semantic ERROR by
default; `--fail-unreadable` remains the single explicit opt-in to fail on
inaccessibility.

## Built-in resolvers

- `stac_integrity.resolvers.planetary_computer_resolver()` — lazily imports
  `planetary_computer` (extra: `pip install "stac-integrity-gate[planetary-computer]"`)
  and returns `lambda href, ctx: planetary_computer.sign(href)`. If the package is
  missing it raises `ResolverUnavailableError` **at creation time**, not per asset.
- `stac_integrity.resolvers.alternate_resolver(name)` — prefer
  `asset["alternate"][name]["href"]` (STAC Alternate Assets extension) when present,
  otherwise the original href. Evidence: CDSE provides `alternate.https` next to
  `s3://eodata/...`; `alternate-https` + a GDAL bearer header is the path of least
  resistance there.
- `identity` — the default.

CLI: `--resolver SPEC` where `SPEC` is a built-in name (`planetary-computer`,
`alternate-<name>`; built-in names contain no `:`) or an import path `module:function`
naming a callable `(href, context) -> str`. Importing a module executes its code, so an
import path has the same trust level as running a Python script you chose; the CLI
never reads a resolver spec from the STAC document.

## Providers without built-in flows (by design)

No credential flows are implemented. Users provide access through GDAL configuration or
their own resolver:

```bash
# Requester-pays S3 (Earth Search NAIP)
AWS_REQUEST_PAYER=requester AWS_PROFILE=me stac-integrity item naip-item.json

# CDSE S3 (keys from the CDSE S3 credentials page)
AWS_S3_ENDPOINT=eodata.dataspace.copernicus.eu AWS_VIRTUAL_HOSTING=FALSE \
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... stac-integrity item cdse-item.json

# CDSE HTTPS alternate with an OIDC token you obtained yourself
GDAL_HTTP_BEARER="$CDSE_TOKEN" stac-integrity item cdse-item.json --resolver alternate-https

# NASA Earthdata (temporary S3 credentials or cookie-based HTTPS)
AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=... stac-integrity item veda-item.json
```

`GDAL_HTTP_HEADERS` / `GDAL_HTTP_HEADER_FILE` also work. A custom resolver can return
pre-signed URLs from any signing service (e.g. an S3 presigner).

## Security

GDAL error messages contain the full URL it tried to open, so without care a SAS
signature (`sig=`, `se=`, `skoid=`…), an AWS presigned signature
(`X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`) or a `token=` would be
copied into `Finding.message` and from there into JSON output, CI logs and artifacts.

Measures:

1. `stac_integrity.redaction.redact()` replaces values of sensitive query parameters,
   URL userinfo (`https://user:pass@`) and `Authorization:`/`Bearer` fragments with
   `REDACTED`. Applied to every exception text that becomes a `Finding.message`
   (`ASSET_UNREADABLE`, `ASSET_RESOLVE_FAILED`), to the CLI's operational-error line
   on stderr, and to hrefs echoed in findings and `source` fields.
2. `Finding.declared` is the **original STAC href**, never the resolved/signed one
   (redacted too, in case the catalog itself publishes signed hrefs).
3. The resolved href is never stored in any result object.
4. The tool never reads, logs or serializes environment variables or HTTP headers;
   credentials stay in GDAL's configuration.

Residual risk: a custom resolver that logs its own output, or a GDAL debug setting
(`CPL_DEBUG=ON`, `CPL_CURL_VERBOSE=YES`) that prints to stderr, is outside the tool's
control and is documented as such.
