"""Zarr semantic checks (optional extra ``stac-integrity-gate[zarr]``).

Only Zarr metadata is read; chunk data is never fetched. ``zarr`` is imported
lazily so the core install keeps Rasterio as its only dependency.
See docs/zarr-design.md for the invariants and severity rationale.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from .audit import Finding, _bands, _dtype_normalize, _effective, _is_remote, _number_equal

INSTALL_HINT = 'pip install "stac-integrity-gate[zarr]"'

_SPATIAL_DIM_PAIRS = {
    ("y", "x"),
    ("lat", "lon"),
    ("latitude", "longitude"),
    ("rows", "cols"),
    ("row", "col"),
}


def is_zarr_asset(asset: dict[str, Any], href: str) -> bool:
    media_type = str(asset.get("type", "")).lower()
    path = urlparse(href).path.lower().rstrip("/")
    return "zarr" in media_type or path.endswith(".zarr") or ".zarr/" in path


def zarr_module() -> Any | None:
    try:
        import zarr  # noqa: PLC0415 - optional dependency, imported lazily
    except ImportError:
        return None
    return zarr


@dataclass
class ArrayFacts:
    """Library-independent view of one Zarr array's metadata."""

    path: str
    shape: tuple[int, ...]
    dtype: str
    fill_value: Any
    attrs: dict[str, Any] = field(default_factory=dict)
    dims: tuple[str, ...] | None = None


def _plain(value: Any) -> Any:
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except Exception:
            return value
    return value


def read_array_facts(node: Any, path: str) -> ArrayFacts:
    import numpy as np  # zarr depends on numpy

    attrs = dict(node.attrs)
    dims = getattr(getattr(node, "metadata", None), "dimension_names", None)
    if not dims:
        dims = attrs.get("_ARRAY_DIMENSIONS")
    return ArrayFacts(
        path=path,
        shape=tuple(int(x) for x in node.shape),
        dtype=str(np.dtype(node.dtype)),
        fill_value=_plain(node.fill_value),
        attrs=attrs,
        dims=tuple(str(d) for d in dims) if dims else None,
    )


# ---------------------------------------------------------------- comparisons


def _first(mappings: list[tuple[dict[str, Any], str]], *names: str) -> tuple[bool, Any, str | None]:
    for mapping, prefix in mappings:
        for name in names:
            if name in mapping:
                return True, mapping[name], f"{prefix}{name}"
    return False, None, None


def _is_active(scale: Any, offset: Any) -> bool:
    try:
        s = 1.0 if scale is None else float(scale)
        o = 0.0 if offset is None else float(offset)
    except (TypeError, ValueError):
        return True  # unparseable: treat as a real declaration
    return not (math.isclose(s, 1.0, abs_tol=1e-12) and math.isclose(o, 0.0, abs_tol=1e-12))


def _is_float(dtype: str) -> bool:
    return dtype.startswith("float") or dtype.startswith("complex")


def _is_int(dtype: str) -> bool:
    return dtype.startswith("int") or dtype.startswith("uint")


def compare_array(
    item: dict[str, Any],
    asset: dict[str, Any],
    key: str,
    facts: ArrayFacts,
    band: dict[str, Any] | None,
    band_label: str,
) -> list[Finding]:
    """Compare one Zarr array with its STAC declaration (band, then asset)."""
    out: list[Finding] = []
    sources: list[tuple[dict[str, Any], str]] = []
    if isinstance(band, dict):
        sources.append((band, f"{band_label}."))
    sources.append((asset, ""))
    where = f" (array {facts.path})"

    # I1 shape
    declared_shape = _effective(item, asset, "proj:shape")
    if declared_shape is not None:
        spatial = len(facts.shape) == 2 or (
            facts.dims is not None
            and len(facts.dims) >= 2
            and tuple(d.lower() for d in facts.dims[-2:]) in _SPATIAL_DIM_PAIRS
        )
        try:
            normalized = [int(declared_shape[0]), int(declared_shape[1])]
            if len(declared_shape) != 2:
                raise ValueError
        except Exception:
            normalized = None
        actual = list(facts.shape[-2:])
        if normalized is None:
            out.append(Finding("ERROR", "SHAPE_INVALID", key, "proj:shape",
                               "proj:shape must contain [height, width].", declared_shape, list(facts.shape)))
        elif not spatial:
            out.append(Finding("WARN", "ZARR_SHAPE_UNVERIFIED", key, "proj:shape",
                               "Zarr array layout has no identifiable [y, x] dimensions; proj:shape was not compared" + where + ".",
                               declared_shape, {"shape": list(facts.shape), "dims": facts.dims}))
        elif normalized != actual:
            out.append(Finding("ERROR", "SHAPE_MISMATCH", key, "proj:shape",
                                "Declared raster shape does not match the Zarr array shape" + where + ".",
                                declared_shape, actual))

    # I2 data type
    has_dtype, declared_dtype, dtype_field = _first(sources, "data_type")
    cf_decoding = any(k in facts.attrs for k in ("scale_factor", "add_offset", "_FillValue"))
    if has_dtype and declared_dtype is not None:
        d = _dtype_normalize(declared_dtype)
        a = _dtype_normalize(facts.dtype)
        if d != a:
            if _is_float(d) and _is_int(a) and cf_decoding:
                out.append(Finding("WARN", "ZARR_DATA_TYPE_DECODED", key, dtype_field,
                                   "Declared floating data type differs from the stored integer type; it may describe "
                                   "CF-decoded values rather than the stored pixels" + where + ".",
                                   declared_dtype, facts.dtype))
            else:
                out.append(Finding("ERROR", "DATA_TYPE_MISMATCH", key, dtype_field,
                                   "Declared data type does not match the Zarr array data type" + where + ".",
                                   declared_dtype, facts.dtype))

    # I3 nodata
    has_nodata, declared_nodata, nodata_field = _first(sources, "nodata")
    if has_nodata and declared_nodata is not None:
        if "_FillValue" in facts.attrs:
            actual_fv = facts.attrs["_FillValue"]
            if not _number_equal(declared_nodata, actual_fv):
                out.append(Finding("ERROR", "NODATA_MISMATCH", key, nodata_field,
                                   "Declared nodata does not match the array's CF _FillValue" + where + ".",
                                   declared_nodata, actual_fv))
        elif facts.fill_value is None:
            out.append(Finding("WARN", "ZARR_NODATA_NOT_IN_STORE", key, nodata_field,
                               "STAC declares nodata but the Zarr array has neither _FillValue nor fill_value" + where + ".",
                               declared_nodata, None))
        elif not _number_equal(declared_nodata, facts.fill_value):
            out.append(Finding("WARN", "ZARR_FILL_VALUE_DIFFERS", key, nodata_field,
                               "Declared nodata differs from the Zarr fill_value (fill_value may be only a storage "
                               "default; no CF _FillValue is present)" + where + ".",
                               declared_nodata, facts.fill_value))

    # I4 scale/offset, double scaling
    has_scale, stac_scale, scale_field = _first(sources, "scale", "raster:scale")
    has_offset, stac_offset, offset_field = _first(sources, "offset", "raster:offset")
    cf_scale = facts.attrs.get("scale_factor")
    cf_offset = facts.attrs.get("add_offset")
    stac_active = (has_scale or has_offset) and _is_active(stac_scale, stac_offset)
    cf_active = (cf_scale is not None or cf_offset is not None) and _is_active(cf_scale, cf_offset)
    if stac_active and cf_active:
        declared = {"scale": stac_scale, "offset": stac_offset}
        actual = {"scale_factor": cf_scale, "add_offset": cf_offset}
        same = _number_equal(1.0 if stac_scale is None else stac_scale, 1.0 if cf_scale is None else cf_scale) and \
            _number_equal(0.0 if stac_offset is None else stac_offset, 0.0 if cf_offset is None else cf_offset)
        fld = scale_field or offset_field
        if same:
            out.append(Finding("WARN", "ZARR_DOUBLE_SCALING_RISK", key, fld,
                               "STAC scale/offset repeats the CF scale_factor/add_offset stored on the Zarr array; "
                               "clients that decode CF attributes (xarray default) and also apply the STAC raster "
                               "scale will scale twice" + where + ".",
                               declared, actual))
        else:
            out.append(Finding("WARN", "ZARR_SCALE_CONFLICT", key, fld,
                               "STAC scale/offset and the Zarr array's CF scale_factor/add_offset both apply "
                               "non-identity scaling with different values; at most one can describe the stored "
                               "pixels" + where + ".",
                               declared, actual))
    return out


# ------------------------------------------------------------- store reading


_GROUP_DECLARATIONS = ("data_type", "nodata", "scale", "offset", "raster:scale", "raster:offset", "proj:shape")


def _list_arrays(group: Any) -> list[str] | None:
    """Child array names, or None when the store cannot list (plain HTTPS)."""
    try:
        return sorted(str(n) for n in group.array_keys())
    except Exception:
        return None


def _resolve_child(zarr: Any, group: Any, name: str) -> tuple[str, Any] | None:
    """Resolve a STAC band name to a child array without relying on listing.

    HTTPS object stores often cannot list (EOPF Sample Service eopf-stac #34),
    so exact / lower-case / upper-case child paths are opened directly first.
    Listing is only used for a *unique* case-insensitive fallback.
    """
    for candidate in dict.fromkeys((name, name.lower(), name.upper())):
        try:
            child = group[candidate]
        except Exception:
            continue
        if isinstance(child, zarr.Array):
            return candidate, child
    names = _list_arrays(group) or []
    matches = [n for n in names if n.lower() == name.lower()]
    if len(matches) == 1:
        return matches[0], group[matches[0]]
    return None


def audit_zarr_asset(
    item: dict[str, Any],
    asset: dict[str, Any],
    key: str,
    href: str,
    source: str,
    unreadable_severity: str = "WARN",
) -> list[Finding]:
    """Check one Zarr asset. Caller must ensure ``zarr_module()`` is not None."""
    zarr = zarr_module()
    try:
        node = zarr.open(href, mode="r")
    except Exception as exc:
        severity = unreadable_severity.upper()
        if severity not in {"WARN", "ERROR"}:
            severity = "WARN"
        if not _is_remote(href) and not _is_remote(source):
            severity = "ERROR"
        return [Finding(severity, "ASSET_UNREADABLE", key, "href",
                        f"Zarr asset could not be opened: {type(exc).__name__}: {exc}",
                        asset.get("href"), None)]

    bands = _bands(asset) or []
    findings: list[Finding] = []
    band_field = "bands" if isinstance(asset.get("bands"), list) else "raster:bands"

    if isinstance(node, zarr.Array):
        facts = read_array_facts(node, "")
        if len(bands) > 1:
            findings.append(Finding("WARN", "ZARR_BANDS_AMBIGUOUS", key, band_field,
                                    "Asset points to a single Zarr array but declares several bands; only "
                                    "asset-level fields were compared.", len(bands), list(facts.shape)))
            findings.extend(compare_array(item, asset, key, facts, None, band_field))
        else:
            band = bands[0] if bands else None
            findings.extend(compare_array(item, asset, key, facts, band, f"{band_field}[0]"))
        return findings

    # Group: resolve STAC band names to child arrays.
    resolved = 0
    unresolved: list[str] = []
    for idx, band in enumerate(bands):
        name = band.get("name") if isinstance(band, dict) else None
        if not name:
            unresolved.append(f"#{idx}")
            continue
        child = _resolve_child(zarr, node, str(name))
        if child is None:
            unresolved.append(str(name))
            continue
        resolved += 1
        facts = read_array_facts(child[1], child[0])
        findings.extend(compare_array(item, asset, key, facts, band, f"{band_field}[{idx}]"))
    declared_fields = [f for f in _GROUP_DECLARATIONS if f in asset]
    if not bands and not declared_fields:
        # e.g. a whole-store "product" asset: nothing is declared to compare.
        return findings
    if resolved == 0:
        findings.append(Finding("WARN", "ZARR_GROUP_UNRESOLVED", key, "href",
                                "Asset points to a Zarr group and no declared band name resolves to a child array; "
                                "the declared band/raster fields could not be compared.",
                                [b.get("name") for b in bands if isinstance(b, dict)] or declared_fields,
                                _list_arrays(node)))
    elif unresolved:
        findings.append(Finding("WARN", "ZARR_BAND_UNRESOLVED", key, band_field,
                                "Some declared bands do not resolve to a child array of the Zarr group and were not checked.",
                                unresolved, _list_arrays(node)))
    return findings


def unavailable_finding(keys: list[str]) -> Finding:
    return Finding("WARN", "ZARR_SUPPORT_UNAVAILABLE", ",".join(keys), "href",
                   f"Zarr assets were skipped because the optional 'zarr' extra is not installed ({INSTALL_HINT}).",
                   keys, None)
