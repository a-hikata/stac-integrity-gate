"""Semantic checks for (Geo)Parquet assets: STAC Table/Projection declarations
versus the Parquet footer.

Only the Parquet footer is read (schema, ``num_rows`` and key/value metadata);
no row data is ever downloaded. ``pyarrow`` is an optional dependency
(``pip install stac-integrity-gate[geoparquet]``) and is imported lazily, so
this module is importable without it.

Severity policy (see docs/geoparquet-design.md):

* ERROR only when the footer was read and it contradicts a declaration made
  *on the asset itself* (``table:columns`` names, ``table:primary_geometry``,
  ``table:row_count``, and CRS when both sides resolve to distinct EPSG codes).
* Declarations inherited from Item properties / Collection fields may describe
  a whole multi-asset table, so contradictions there are WARN.
* Type comparisons, case-only name differences, partition keys, geometry
  metadata disagreements and anything unreadable are WARN.
"""

from __future__ import annotations

import io
import json
import re
import urllib.request
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

PARQUET_MEDIA_TYPES = (
    "application/vnd.apache.parquet",
    "application/x-parquet",
    "application/parquet",
)


def is_parquet_asset(asset: dict[str, Any], href: str) -> bool:
    media_type = str(asset.get("type", "")).lower()
    path = urlparse(href).path.lower()
    return (
        any(media_type.startswith(t) for t in PARQUET_MEDIA_TYPES)
        or "geoparquet" in media_type
        or path.endswith(".parquet")
        or path.endswith(".geoparquet")
    )


def pyarrow_available() -> bool:
    try:
        import pyarrow.parquet  # noqa: F401
    except Exception:
        return False
    return True


# --------------------------------------------------------------------------
# Remote footer access without fsspec: a minimal seekable HTTP range reader.
# --------------------------------------------------------------------------


class _HTTPRangeFile(io.RawIOBase):
    """Read-only seekable file over HTTP Range requests (footer reads only)."""

    def __init__(self, url: str, user_agent: str, timeout: float = 30.0):
        super().__init__()
        self.url = url
        self.headers = {"User-Agent": user_agent}
        self.timeout = timeout
        self.pos = 0
        # Suffix range: learn the size and prefetch the tail (footer) at once.
        tail, total = self._fetch("bytes=-65536")
        if total is None:
            raise OSError("server did not honour HTTP Range requests")
        self.size = total
        self._cache_start = total - len(tail)
        self._cache = tail

    def _fetch(self, range_header: str) -> tuple[bytes, int | None]:
        req = urllib.request.Request(self.url, headers={**self.headers, "Range": range_header})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = resp.read()
            content_range = resp.headers.get("Content-Range")
            if resp.status != 206 or not content_range:
                return data, None
            match = re.search(r"/(\d+)\s*$", content_range)
            return data, int(match.group(1)) if match else None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self.pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            self.pos = offset
        elif whence == io.SEEK_CUR:
            self.pos += offset
        elif whence == io.SEEK_END:
            self.pos = self.size + offset
        return self.pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self.size - self.pos
        n = max(0, min(n, self.size - self.pos))
        if n == 0:
            return b""
        start, end = self.pos, self.pos + n
        if start >= self._cache_start and end <= self._cache_start + len(self._cache):
            data = self._cache[start - self._cache_start:end - self._cache_start]
        else:
            data, _ = self._fetch(f"bytes={start}-{end - 1}")
            data = data[:n]
        self.pos += len(data)
        return data

    def readinto(self, b: Any) -> int:
        data = self.read(len(b))
        b[: len(data)] = data
        return len(data)


def _open_footer(href: str, user_agent: str) -> Any:
    """Return pyarrow FileMetaData for a single Parquet file."""
    import pyarrow.parquet as pq

    scheme = urlparse(href).scheme.lower()
    if scheme in {"http", "https"}:
        with _HTTPRangeFile(href, user_agent) as fh:
            return pq.ParquetFile(fh).metadata
    if scheme and len(scheme) > 1 and scheme != "file":
        # s3://, gs://, abfs:// ... via pyarrow's own filesystems (credentials
        # come from the environment; failures are reported as access problems).
        return pq.ParquetFile(href).metadata
    path = unquote(urlparse(href).path) if scheme == "file" else href
    return pq.ParquetFile(path).metadata


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _declared(item: dict[str, Any], asset: dict[str, Any], field: str) -> tuple[Any, bool]:
    """Return (value, declared_on_asset)."""
    if field in asset:
        return asset[field], True
    return item.get("properties", {}).get(field), False


def _sev(on_asset: bool) -> str:
    return "ERROR" if on_asset else "WARN"


def _type_family(value: str) -> str | None:
    t = str(value).strip().lower()
    t = re.sub(r"\s+", "", t)
    if not t:
        return None
    if t in {"number", "numeric"}:
        return "number"  # JSON-Schema style: any integer, float or decimal
    if re.fullmatch(r"u?int(8|16|32|64)?|integer|long|short|byte|ubyte|bigint|smallint|tinyint", t):
        return "integer"
    if re.fullmatch(r"float(16|32|64)?|double|halffloat|real", t):
        return "float"
    if t.startswith("decimal"):
        return "decimal"
    if t in {"string", "str", "utf8", "large_string", "largeutf8", "large_utf8", "text", "varchar", "string_view"}:
        return "string"
    if t in {"bool", "boolean"}:
        return "boolean"
    if t.startswith("date") and not t.startswith("datetime"):
        return "date"
    if t.startswith("timestamp") or t.startswith("datetime"):
        return "timestamp"
    if t.startswith("time"):
        return "time"
    if t in {"binary", "large_binary", "bytes", "blob", "wkb", "binary_view"} or t.startswith("fixed_size_binary"):
        return "binary"
    if t.startswith("struct") or t.startswith("list") or t.startswith("large_list") or t.startswith("map") or t.endswith("[]"):
        return "nested"
    return None  # unknown vocabulary (e.g. "geometry", "object"): not compared


def _types_compatible(declared_family: str, actual_family: str) -> bool:
    if declared_family == actual_family:
        return True
    if declared_family == "number" and actual_family in {"integer", "float", "decimal"}:
        return True
    return False


_CRS84_ALIASES = {"OGC:CRS84": 4326, "OGC:CRS83": 4269, "OGC:CRS27": 4267}


def _crs_epsg(crs: Any) -> int | None:
    """EPSG code with lon/lat CRS84-style variants folded onto their EPSG twin.

    GeoParquet stores coordinates in x/y (lon/lat) order regardless of the CRS
    definition, so OGC:CRS84 and EPSG:4326 describe the same data here.
    """
    try:
        code = crs.to_epsg()
    except Exception:
        code = None
    if code is not None:
        return int(code)
    try:
        auth = crs.to_authority()
    except Exception:
        auth = None
    if auth:
        return _CRS84_ALIASES.get(f"{auth[0]}:{auth[1]}".upper())
    return None


def _parse_crs(value: Any) -> Any:
    from rasterio.crs import CRS

    if isinstance(value, dict):
        value = json.dumps(value)
    return CRS.from_user_input(value)


# --------------------------------------------------------------------------
# Individual invariants
# --------------------------------------------------------------------------


def _check_columns(item, asset, key, href, schema, geo) -> Iterable[Any]:
    from .audit import Finding

    declared, on_asset = _declared(item, asset, "table:columns")
    if declared is None:
        return
    if not isinstance(declared, list):
        yield Finding("WARN", "TABLE_COLUMNS_INVALID", key, "table:columns",
                      "table:columns must be a list of column objects; columns were not checked.",
                      declared, None)
        return
    actual_names = list(schema.names)
    actual_set = set(actual_names)
    lower_map: dict[str, str] = {}
    for name in actual_names:
        lower_map.setdefault(name.lower(), name)
    partition_keys = {m.group(1) for m in re.finditer(r"/([^/=]+)=[^/]*", urlparse(href).path)}
    geo_columns = set((geo or {}).get("columns", {}) or {}) if isinstance(geo, dict) else set()

    for idx, col in enumerate(declared):
        if not isinstance(col, dict) or not isinstance(col.get("name"), str):
            continue
        name = col["name"]
        field = f"table:columns[{idx}]"
        if name not in actual_set:
            if "." in name and name.split(".", 1)[0] in actual_set:
                continue  # nested struct path; top-level column exists
            if name in partition_keys:
                yield Finding("WARN", "TABLE_COLUMN_PARTITION_KEY", key, f"{field}.name",
                              "Declared column is a Hive partition key in the href; it is not stored in the file schema.",
                              name, None)
            elif name.lower() in lower_map:
                yield Finding("WARN", "TABLE_COLUMN_CASE_MISMATCH", key, f"{field}.name",
                              "Declared column differs from the Parquet column only by case; case-sensitive readers (Arrow, pandas) will not find it.",
                              name, lower_map[name.lower()])
            else:
                yield Finding(_sev(on_asset), "TABLE_COLUMN_MISSING", key, f"{field}.name",
                              "Declared column does not exist in the Parquet schema."
                              + ("" if on_asset else " (declaration inherited from Item/Collection; it may describe another asset)"),
                              name, actual_names)
            continue

        declared_type = col.get("type")
        if declared_type is None:
            continue
        arrow_type = schema.field(name).type
        dfam = _type_family(declared_type)
        afam = _type_family(str(arrow_type))
        if dfam is None or afam is None:
            continue
        if name in geo_columns:
            continue  # geometry encodings (WKB binary / GeoArrow nested) vary by design
        if not _types_compatible(dfam, afam):
            yield Finding("WARN", "TABLE_COLUMN_TYPE_MISMATCH", key, f"{field}.type",
                          f"Declared column type ({dfam}) differs from the Parquet/Arrow type ({afam}); the Table extension type vocabulary is free-form, so this is not treated as an error.",
                          declared_type, str(arrow_type))


def _check_row_count(item, asset, key, metadata) -> Iterable[Any]:
    from .audit import Finding

    declared, on_asset = _declared(item, asset, "table:row_count")
    if declared is None:
        return
    if isinstance(declared, bool) or not isinstance(declared, (int, float)) or (
        isinstance(declared, float) and not declared.is_integer()
    ):
        yield Finding("WARN", "TABLE_ROW_COUNT_INVALID", key, "table:row_count",
                      "table:row_count is not an integer; it was not compared.", declared, metadata.num_rows)
        return
    if int(declared) != metadata.num_rows:
        yield Finding(_sev(on_asset), "TABLE_ROW_COUNT_MISMATCH", key, "table:row_count",
                      "Declared row count does not match num_rows in the Parquet footer."
                      + ("" if on_asset else " (declaration inherited from Item/Collection; it may count several assets)"),
                      declared, metadata.num_rows)


def _check_geometry(item, asset, key, schema, geo, geo_error) -> Iterable[Any]:
    from .audit import Finding

    declared, on_asset = _declared(item, asset, "table:primary_geometry")
    media_type = str(asset.get("type", "")).lower()
    claims_geo = declared is not None or "geoparquet" in media_type

    if geo_error:
        yield Finding("WARN", "GEOPARQUET_METADATA_INVALID", key, "geo",
                      f"Parquet 'geo' metadata could not be parsed: {geo_error}", None, None)
        return
    if geo is None:
        if claims_geo:
            yield Finding("WARN", "GEOPARQUET_METADATA_MISSING", key, "geo",
                          "Asset is declared as geospatial but the Parquet footer has no GeoParquet 'geo' metadata.",
                          declared, None)
        if declared is not None and declared not in set(schema.names):
            yield Finding(_sev(on_asset), "PRIMARY_GEOMETRY_MISSING", key, "table:primary_geometry",
                          "Declared primary geometry column does not exist in the Parquet schema.",
                          declared, list(schema.names))
        return

    if declared is None:
        return
    if declared not in set(schema.names):
        yield Finding(_sev(on_asset), "PRIMARY_GEOMETRY_MISSING", key, "table:primary_geometry",
                      "Declared primary geometry column does not exist in the Parquet schema.",
                      declared, list(schema.names))
        return
    geo_columns = geo.get("columns") or {}
    primary = geo.get("primary_column")
    if declared not in geo_columns:
        yield Finding("WARN", "PRIMARY_GEOMETRY_NOT_GEOMETRY", key, "table:primary_geometry",
                      "Declared primary geometry column exists but is not listed as a geometry column in GeoParquet metadata.",
                      declared, sorted(geo_columns))
    elif primary != declared:
        yield Finding("WARN", "PRIMARY_GEOMETRY_DIFFERS", key, "table:primary_geometry",
                      "Declared primary geometry differs from GeoParquet primary_column (both are geometry columns).",
                      declared, primary)


def _check_crs(item, asset, key, geo) -> Iterable[Any]:
    from .audit import Finding

    if not isinstance(geo, dict):
        return
    primary = geo.get("primary_column")
    col_meta = (geo.get("columns") or {}).get(primary)
    if not isinstance(col_meta, dict):
        return

    for field in ("proj:code", "proj:epsg", "proj:wkt2", "proj:projjson"):
        declared, on_asset = _declared(item, asset, field)
        if declared is None:
            continue
        if field == "proj:epsg" and isinstance(declared, float) and declared.is_integer():
            declared = int(declared)
        text = f"EPSG:{declared}" if field == "proj:epsg" else declared
        try:
            declared_crs = _parse_crs(text)
        except Exception:
            yield Finding("WARN", "CRS_UNPARSEABLE", key, field,
                          "Declared CRS could not be parsed by Rasterio/PROJ; equivalence was not checked.",
                          declared, None)
            continue

        if "crs" not in col_meta:
            actual_raw: Any = "OGC:CRS84"  # GeoParquet default when the key is absent
        else:
            actual_raw = col_meta["crs"]
        if actual_raw is None:
            yield Finding("WARN", "GEOPARQUET_CRS_UNDEFINED", key, field,
                          "GeoParquet metadata sets crs to null (undefined CRS); the declared CRS cannot be verified.",
                          declared, None)
            continue
        try:
            actual_crs = _parse_crs(actual_raw)
        except Exception:
            yield Finding("WARN", "GEOPARQUET_CRS_UNPARSEABLE", key, field,
                          "GeoParquet CRS could not be parsed by Rasterio/PROJ; equivalence was not checked.",
                          declared, actual_raw if isinstance(actual_raw, str) else "<PROJJSON>")
            continue

        if declared_crs == actual_crs:
            continue
        d_code, a_code = _crs_epsg(declared_crs), _crs_epsg(actual_crs)
        if d_code is not None and d_code == a_code:
            continue  # e.g. EPSG:4326 vs OGC:CRS84: GeoParquet axis order is always x/y
        actual_str = actual_crs.to_string()
        if d_code is not None and a_code is not None and on_asset:
            yield Finding("ERROR", "CRS_MISMATCH", key, field,
                          "Declared CRS is not equivalent to the GeoParquet 'geo' CRS of the primary geometry column.",
                          declared, actual_str)
        else:
            yield Finding("WARN", "CRS_MISMATCH_UNCERTAIN", key, field,
                          "Declared CRS does not compare equal to the GeoParquet CRS; equivalence could not be established with certainty.",
                          declared, actual_str)


# --------------------------------------------------------------------------
# Entry point used by audit.audit_item_dict
# --------------------------------------------------------------------------


def audit_parquet_asset(
    item: dict[str, Any],
    asset: dict[str, Any],
    key: str,
    href: str,
    *,
    source: str,
    unreadable_severity: str = "WARN",
) -> tuple[bool, list[Any]]:
    """Return (checked, findings) for one Parquet asset."""
    from .audit import USER_AGENT, Finding, _is_remote

    raw_href = asset.get("href")
    if not pyarrow_available():
        return False, [Finding("WARN", "GEOPARQUET_DEPENDENCY_MISSING", key, "href",
                               "Parquet asset skipped: install 'stac-integrity-gate[geoparquet]' (pyarrow) to verify table/GeoParquet declarations.",
                               raw_href, None)]
    path = urlparse(href).path
    if any(ch in href for ch in "*?[") or path.endswith("/"):
        return False, [Finding("WARN", "PARQUET_PARTITIONED_UNVERIFIED", key, "href",
                               "Asset href is a glob/directory (partitioned dataset); individual files were not inspected.",
                               raw_href, None)]

    try:
        metadata = _open_footer(href, USER_AGENT)
        schema = metadata.schema.to_arrow_schema()
    except Exception as exc:
        severity = unreadable_severity.upper()
        if severity not in {"WARN", "ERROR"}:
            severity = "WARN"
        if not _is_remote(href) and not _is_remote(source):
            severity = "ERROR"  # same rule as raster assets: local catalog, local file missing
        return True, [Finding(severity, "ASSET_UNREADABLE", key, "href",
                              f"Parquet asset footer could not be read: {type(exc).__name__}: {exc}",
                              raw_href, None)]

    geo: Any = None
    geo_error: str | None = None
    kv = metadata.metadata or {}
    if b"geo" in kv:
        try:
            geo = json.loads(kv[b"geo"])
            if not isinstance(geo, dict):
                raise ValueError("not a JSON object")
        except Exception as exc:
            geo, geo_error = None, f"{type(exc).__name__}: {exc}"

    findings: list[Any] = []
    findings.extend(_check_columns(item, asset, key, href, schema, geo))
    findings.extend(_check_row_count(item, asset, key, metadata))
    findings.extend(_check_geometry(item, asset, key, schema, geo, geo_error))
    findings.extend(_check_crs(item, asset, key, geo))
    return True, findings
