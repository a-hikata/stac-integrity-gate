from __future__ import annotations

import json
import math
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import rasterio
from rasterio.crs import CRS

from . import __version__

USER_AGENT = f"stac-integrity-gate/{__version__}"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    asset: str
    field: str
    message: str
    declared: Any = None
    actual: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AuditResult:
    source: str
    item_id: str
    checked_assets: int
    skipped_assets: int
    findings: list[Finding]

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "WARN"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "item_id": self.item_id,
            "checked_assets": self.checked_assets,
            "skipped_assets": self.skipped_assets,
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "findings": [f.to_dict() for f in self.findings],
        }


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _is_remote(value: str) -> bool:
    # Object-store URIs (s3://, gs://, az://, ...) are remote: failing to open
    # them is an access problem, not proof that a local file is missing.
    # Windows drive letters parse as a 1-char scheme.
    scheme = urlparse(value).scheme.lower()
    return len(scheme) > 1 and scheme != "file"


def load_json(source: str) -> dict[str, Any]:
    if _is_url(source):
        req = urllib.request.Request(source, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as response:
            return json.load(response)
    return json.loads(Path(source).read_text())


def _resolve_href(item_source: str, href: str) -> str:
    parsed = urlparse(href)
    if parsed.scheme:
        return href
    if _is_url(item_source):
        return urljoin(item_source, href)
    return str((Path(item_source).resolve().parent / href).resolve())


def _effective(item: dict[str, Any], asset: dict[str, Any], field: str) -> Any:
    if field in asset:
        return asset[field]
    return item.get("properties", {}).get(field)



def _parse_crs(declared: Any) -> CRS | None:
    try:
        return CRS.from_user_input(declared)
    except Exception:
        return None


def _horizontal_crs(crs: CRS) -> CRS | None:
    """Return the horizontal part of a compound CRS (WKT1 COMPD_CS), else None."""
    wkt = crs.to_wkt()
    if not wkt.startswith("COMPD_CS["):
        return None
    # COMPD_CS["name",<horizontal CRS>,<vertical CRS>]: take the first sub-CRS.
    start = wkt.find(",", len("COMPD_CS[")) + 1
    depth = 0
    for i in range(start, len(wkt)):
        if wkt[i] == "[":
            depth += 1
        elif wkt[i] == "]":
            depth -= 1
            if depth == 0:
                try:
                    return CRS.from_wkt(wkt[start:i + 1])
                except Exception:
                    return None
    return None


def _normalize_transform(value: Any) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) not in (6, 9):
        return None
    try:
        values = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    if len(values) == 9 and any(
        not math.isclose(v, e, abs_tol=1e-12, rel_tol=0)
        for v, e in zip(values[6:], (0.0, 0.0, 1.0))
    ):
        return None
    return values[:6]


def _normalize_special_number(value: Any) -> Any:
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"nan", "+nan", "-nan"}:
            return float("nan")
        if v in {"inf", "+inf", "infinity", "+infinity"}:
            return float("inf")
        if v in {"-inf", "-infinity"}:
            return float("-inf")
    return value


def _number_equal(a: Any, b: Any, abs_tol: float = 1e-9, rel_tol: float = 1e-9) -> bool:
    a = _normalize_special_number(a)
    b = _normalize_special_number(b)
    try:
        af = float(a)
        bf = float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(af) and math.isnan(bf):
        return True
    return math.isclose(af, bf, abs_tol=abs_tol, rel_tol=rel_tol)


def _dtype_normalize(value: str) -> str:
    aliases = {
        "byte": "uint8",
        "ubyte": "uint8",
        "ushort": "uint16",
        "short": "int16",
        "uint": "uint32",
        "int": "int32",
        "float": "float32",
        "double": "float64",
    }
    v = str(value).strip().lower()
    return aliases.get(v, v)


def _bands(asset: dict[str, Any]) -> list[dict[str, Any]] | None:
    # STAC 1.1/Core Bands (and Raster v2) first; Raster v1 fallback.
    value = asset.get("bands")
    if isinstance(value, list):
        return value
    value = asset.get("raster:bands")
    if isinstance(value, list):
        return value
    return None


def _check_crs(item: dict[str, Any], asset: dict[str, Any], key: str, src: Any) -> Iterable[Finding]:
    candidates = [
        ("proj:code", _effective(item, asset, "proj:code")),
        ("proj:epsg", _effective(item, asset, "proj:epsg")),
        ("proj:wkt2", _effective(item, asset, "proj:wkt2")),
        ("proj:projjson", _effective(item, asset, "proj:projjson")),
    ]
    actual = src.crs
    for field, declared in candidates:
        if declared is None:
            continue
        if field == "proj:epsg" and isinstance(declared, float) and declared.is_integer():
            declared = int(declared)  # JSON 4326.0 is the integer code 4326
        crs_declared = f"EPSG:{declared}" if field == "proj:epsg" else declared
        parsed = _parse_crs(crs_declared)
        if parsed is None:
            yield Finding(
                "WARN",
                "CRS_UNPARSEABLE",
                key,
                field,
                "Declared CRS could not be parsed by Rasterio/PROJ; equivalence was not checked.",
                declared,
                actual.to_string() if actual else None,
            )
        elif actual is None:
            yield Finding(
                "ERROR",
                "RASTER_CRS_MISSING",
                key,
                field,
                "STAC declares a CRS but the raster header has none.",
                declared,
                None,
            )
        elif parsed != actual and _horizontal_crs(parsed) == actual:
            yield Finding(
                "WARN",
                "CRS_VERTICAL_UNVERIFIED",
                key,
                field,
                "Declared compound CRS has the raster's horizontal CRS; its vertical component cannot be verified from the raster header.",
                declared,
                actual.to_string(),
            )
        elif parsed != actual:
            yield Finding(
                "ERROR",
                "CRS_MISMATCH",
                key,
                field,
                "Declared CRS is not equivalent to the raster CRS.",
                declared,
                actual.to_string(),
            )


def _check_shape(item: dict[str, Any], asset: dict[str, Any], key: str, src: Any) -> Iterable[Finding]:
    declared = _effective(item, asset, "proj:shape")
    if declared is None:
        return
    actual = [src.height, src.width]
    try:
        if len(declared) != 2:
            raise ValueError
        normalized = [int(declared[0]), int(declared[1])]
    except Exception:
        yield Finding(
            "ERROR",
            "SHAPE_INVALID",
            key,
            "proj:shape",
            "proj:shape must contain [height, width].",
            declared,
            actual,
        )
        return
    if normalized != actual:
        yield Finding(
            "ERROR",
            "SHAPE_MISMATCH",
            key,
            "proj:shape",
            "Declared raster shape does not match the asset header.",
            declared,
            actual,
        )


def _check_transform(
    item: dict[str, Any], asset: dict[str, Any], key: str, src: Any, tolerance_px: float
) -> Iterable[Finding]:
    declared_raw = _effective(item, asset, "proj:transform")
    if declared_raw is None:
        return
    declared = _normalize_transform(declared_raw)
    if declared is None:
        yield Finding(
            "ERROR",
            "TRANSFORM_INVALID",
            key,
            "proj:transform",
            "Projection transform must contain 6 coefficients, or 9 with a [0,0,1] affine tail.",
            declared_raw,
            None,
        )
        return

    actual = [float(x) for x in list(src.transform)[:6]]
    pixel = max(abs(src.transform.a), abs(src.transform.e), 1e-12)
    coeff_tol = pixel * 1e-7
    origin_tol = pixel * tolerance_px
    tolerances = [coeff_tol, coeff_tol, origin_tol, coeff_tol, coeff_tol, origin_tol]
    if any(not _number_equal(d, a, abs_tol=t) for d, a, t in zip(declared, actual, tolerances)):
        yield Finding(
            "ERROR",
            "TRANSFORM_MISMATCH",
            key,
            "proj:transform",
            f"Declared affine transform differs from raster header beyond {tolerance_px:g} pixel origin tolerance.",
            declared_raw,
            actual,
        )


def _check_bbox(
    item: dict[str, Any], asset: dict[str, Any], key: str, src: Any, tolerance_px: float
) -> Iterable[Finding]:
    declared = _effective(item, asset, "proj:bbox")
    if declared is None:
        return
    if not isinstance(declared, (list, tuple)) or len(declared) != 4:
        yield Finding(
            "ERROR",
            "BBOX_INVALID",
            key,
            "proj:bbox",
            "proj:bbox must contain [xmin, ymin, xmax, ymax].",
            declared,
            None,
        )
        return
    actual = [src.bounds.left, src.bounds.bottom, src.bounds.right, src.bounds.top]
    pixel = max(abs(src.transform.a), abs(src.transform.e), 1e-12)
    abs_tol = pixel * tolerance_px
    try:
        comparable = [float(x) for x in declared]
    except Exception:
        comparable = list(declared)
    if any(not _number_equal(d, a, abs_tol=abs_tol) for d, a in zip(comparable, actual)):
        yield Finding(
            "ERROR",
            "BBOX_MISMATCH",
            key,
            "proj:bbox",
            f"Declared projected bbox differs from raster bounds beyond {tolerance_px:g} pixel tolerance.",
            declared,
            actual,
        )


def _check_bands(asset: dict[str, Any], key: str, src: Any) -> Iterable[Finding]:
    bands = _bands(asset)
    if bands is None:
        return

    if len(bands) != src.count:
        yield Finding(
            "ERROR",
            "BAND_COUNT_MISMATCH",
            key,
            "bands",
            "Declared band count does not match the raster band count.",
            len(bands),
            src.count,
        )

    for idx, band in enumerate(bands[: src.count]):
        if not isinstance(band, dict):
            continue
        declared_dtype = band.get("data_type")
        if declared_dtype is not None:
            actual_dtype = src.dtypes[idx]
            if _dtype_normalize(declared_dtype) != _dtype_normalize(actual_dtype):
                yield Finding(
                    "ERROR",
                    "DATA_TYPE_MISMATCH",
                    key,
                    f"bands[{idx}].data_type",
                    "Declared band data type does not match the raster.",
                    declared_dtype,
                    actual_dtype,
                )

        if "nodata" in band:
            declared_nodata = band.get("nodata")
            actual_nodata = src.nodatavals[idx]
            if declared_nodata is None and actual_nodata is None:
                pass
            elif actual_nodata is None:
                # The header carries no nodata tag. That does not prove the STAC
                # value wrong (the fill value may still be used in the pixels).
                yield Finding(
                    "WARN",
                    "NODATA_NOT_IN_HEADER",
                    key,
                    f"bands[{idx}].nodata",
                    "STAC declares nodata but the raster header has no nodata value; the declaration cannot be verified.",
                    declared_nodata,
                    None,
                )
            elif not _number_equal(declared_nodata, actual_nodata):
                yield Finding(
                    "ERROR",
                    "NODATA_MISMATCH",
                    key,
                    f"bands[{idx}].nodata",
                    "Declared nodata does not match the raster band nodata.",
                    declared_nodata,
                    actual_nodata,
                )

        # Scale/offset are warnings: STAC may intentionally describe semantic
        # scaling even when GeoTIFF tags do not physically store it.
        if "scale" in band:
            declared_scale = band.get("scale")
            actual_scale = src.scales[idx]
            if not _number_equal(declared_scale, actual_scale):
                yield Finding(
                    "WARN",
                    "SCALE_MISMATCH",
                    key,
                    f"bands[{idx}].scale",
                    "Declared scale differs from raster header scale.",
                    declared_scale,
                    actual_scale,
                )
        if "offset" in band:
            declared_offset = band.get("offset")
            actual_offset = src.offsets[idx]
            if not _number_equal(declared_offset, actual_offset):
                yield Finding(
                    "WARN",
                    "OFFSET_MISMATCH",
                    key,
                    f"bands[{idx}].offset",
                    "Declared offset differs from raster header offset.",
                    declared_offset,
                    actual_offset,
                )


def _is_raster_asset(asset: dict[str, Any], href: str) -> bool:
    media_type = str(asset.get("type", "")).lower()
    path = urlparse(href).path.lower()
    return (
        "tiff" in media_type
        or "geotiff" in media_type
        or "jp2" in media_type
        or "jpeg2000" in media_type
        or path.endswith(".tif")
        or path.endswith(".tiff")
        or path.endswith(".jp2")
    )


def _is_data_asset(asset: dict[str, Any]) -> bool:
    roles = asset.get("roles")
    if not roles:
        return True
    roles_l = {str(x).lower() for x in roles}
    if "data" in roles_l:
        return True
    return False


def audit_item_dict(
    item: dict[str, Any],
    *,
    source: str = "<memory>",
    asset_keys: list[str] | None = None,
    tolerance_px: float = 0.01,
    data_assets_only: bool = True,
    unreadable_severity: str = "WARN",
) -> AuditResult:
    item_id = str(item.get("id", "<unknown>"))
    findings: list[Finding] = []
    checked = 0
    skipped = 0

    assets = item.get("assets") or {}
    if not isinstance(assets, dict):
        raise ValueError("STAC item 'assets' must be an object")

    selected = set(asset_keys or assets.keys())
    # Metadata-only relation check: two distinct data assets pointing at the
    # exact same href is often a publishing error (e.g. polarizations/bands
    # collapsed onto one group). It can be intentional, so this is WARN only.
    href_groups: dict[str, list[str]] = {}
    for key, raw_asset in assets.items():
        if key not in selected or not isinstance(raw_asset, dict):
            continue
        # Collection item_assets are definitions, not inherited defaults.
        # Only fields actually present on the Item asset apply to that asset.
        if not _is_data_asset(raw_asset) or not raw_asset.get("href"):
            continue
        normalized_href = _resolve_href(source, str(raw_asset["href"]))
        href_groups.setdefault(normalized_href, []).append(str(key))
    for href, keys in href_groups.items():
        if len(keys) > 1:
            findings.append(
                Finding(
                    "WARN",
                    "DUPLICATE_DATA_HREF",
                    ",".join(sorted(keys)),
                    "href",
                    "Multiple data assets point to the same href; verify that they are intentional aliases.",
                    keys,
                    href,
                )
            )

    for key, raw_asset in assets.items():
        if key not in selected or not isinstance(raw_asset, dict):
            continue
        # Do not merge Collection item_assets into Item assets. STAC requires
        # Collection definitions to be repeated on matching Item assets; absent
        # properties do not apply to the Item asset.
        asset = dict(raw_asset)
        href = asset.get("href")
        if not href:
            skipped += 1
            continue
        href = _resolve_href(source, str(href))
        if not _is_raster_asset(asset, href):
            skipped += 1
            continue
        if data_assets_only and not _is_data_asset(asset):
            skipped += 1
            continue

        checked += 1
        try:
            env_kwargs = {}
            if _is_remote(href):
                env_kwargs = {
                    "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
                    "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif,.tiff,.jp2",
                }
            with rasterio.Env(**env_kwargs):
                with rasterio.open(href) as src:
                    findings.extend(_check_crs(item, asset, key, src))
                    findings.extend(_check_shape(item, asset, key, src))
                    findings.extend(_check_transform(item, asset, key, src, tolerance_px))
                    findings.extend(_check_bbox(item, asset, key, src, tolerance_px))
                    findings.extend(_check_bands(asset, key, src))
        except Exception as exc:
            severity = unreadable_severity.upper()
            if severity not in {"WARN", "ERROR"}:
                severity = "WARN"
            # A missing local file is a publishing error only when the catalog
            # itself is local. A remote catalog's file:// hrefs point at the
            # publisher's filesystem, so failing to open them is access-only.
            if not _is_remote(href) and not _is_remote(source):
                severity = "ERROR"
            findings.append(
                Finding(
                    severity,
                    "ASSET_UNREADABLE",
                    key,
                    "href",
                    f"Raster asset could not be opened: {type(exc).__name__}: {exc}",
                    raw_asset.get("href"),
                    None,
                )
            )

    if checked == 0:
        findings.append(
            Finding(
                "WARN",
                "NO_RASTER_ASSETS",
                "",
                "assets",
                "No selected data-role GeoTIFF/COG assets were available for semantic inspection.",
                None,
                None,
            )
        )

    return AuditResult(
        source=source,
        item_id=item_id,
        checked_assets=checked,
        skipped_assets=skipped,
        findings=findings,
    )


def audit_item(
    source: str,
    *,
    asset_keys: list[str] | None = None,
    tolerance_px: float = 0.01,
    data_assets_only: bool = True,
    unreadable_severity: str = "WARN",
) -> AuditResult:
    item = load_json(source)
    return audit_item_dict(
        item,
        source=source,
        asset_keys=asset_keys,
        tolerance_px=tolerance_px,
        data_assets_only=data_assets_only,
        unreadable_severity=unreadable_severity,
    )
