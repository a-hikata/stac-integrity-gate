"""Report renderers: text, JSON, SARIF 2.1.0 and JUnit XML.

All renderers are pure functions of an ``AuditResult`` / ``CollectionAuditResult``
plus the ``strict`` flag. They never change the audit verdict or the exit code;
``--strict`` only affects the gate status (``status`` / JUnit failures), never
the per-finding severity.

Only the standard library is used (``json`` and ``xml.etree``).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any, Union
from urllib.parse import urlparse

from . import __version__
from .audit import AuditResult, Finding
from .collection import CollectionAuditResult

Result = Union[AuditResult, CollectionAuditResult]

FORMATS = ("text", "json", "sarif", "junit")

#: Version of the JSON report contract (``--format json``). Bump the minor
#: version for additive changes and the major version for breaking ones.
SCHEMA_VERSION = "1.0"

TOOL_NAME = "stac-integrity-gate"
SARIF_SCHEMA = "https://json.schemastore.org/sarif-2.1.0.json"

#: Finding-code catalog: code -> (default severity, short description).
#: Unknown codes (e.g. added by a newer audit module) still render; they get a
#: generic description and the severity of the first finding that used them.
RULES: dict[str, tuple[str, str]] = {
    "ASSET_UNREADABLE": ("WARN", "Raster asset could not be opened (access/network failure, not a semantic failure)."),
    "BAND_COUNT_MISMATCH": ("ERROR", "Number of declared bands differs from the raster band count."),
    "BBOX_INVALID": ("ERROR", "Declared proj:bbox is malformed."),
    "BBOX_MISMATCH": ("ERROR", "Declared proj:bbox differs from the raster bounds."),
    "CRS_MISMATCH": ("ERROR", "Declared CRS differs from the raster CRS."),
    "CRS_UNPARSEABLE": ("WARN", "Declared CRS could not be parsed; CRS not verified."),
    "CRS_VERTICAL_UNVERIFIED": ("WARN", "Declared compound/vertical CRS component could not be verified from the header."),
    "DATA_TYPE_MISMATCH": ("ERROR", "Declared raster:bands data_type differs from the raster data type."),
    "DUPLICATE_DATA_HREF": ("WARN", "Multiple data assets point to the same href."),
    "NODATA_MISMATCH": ("ERROR", "Declared nodata differs from the raster nodata value."),
    "NODATA_NOT_IN_HEADER": ("WARN", "Declared nodata is not recorded in the raster header."),
    "NO_RASTER_ASSETS": ("WARN", "No selected data-role GeoTIFF/COG assets were available for inspection."),
    "OFFSET_MISMATCH": ("WARN", "Declared offset differs from the raster header offset."),
    "RASTER_CRS_MISSING": ("ERROR", "A CRS is declared but the raster has no CRS."),
    "SCALE_MISMATCH": ("WARN", "Declared scale differs from the raster header scale."),
    "SHAPE_INVALID": ("ERROR", "Declared proj:shape is malformed."),
    "SHAPE_MISMATCH": ("ERROR", "Declared proj:shape differs from the raster height/width."),
    "TRANSFORM_INVALID": ("ERROR", "Declared proj:transform is malformed."),
    "TRANSFORM_MISMATCH": ("ERROR", "Declared proj:transform differs from the raster geotransform."),
}

_SARIF_LEVEL = {"ERROR": "error", "WARN": "warning"}


# --------------------------------------------------------------------------- helpers
def _results(result: Result) -> list[AuditResult]:
    if isinstance(result, CollectionAuditResult):
        return result._all_results()
    return [result]


def _findings(result: Result) -> list[tuple[AuditResult, Finding]]:
    return [(r, f) for r in _results(result) for f in r.findings]


def error_count(result: Result) -> int:
    return result.error_count if isinstance(result, CollectionAuditResult) else len(result.errors)


def warning_count(result: Result) -> int:
    return result.warning_count if isinstance(result, CollectionAuditResult) else len(result.warnings)


def gate_failed(result: Result, strict: bool) -> bool:
    """The CI verdict: any ERROR, or any WARN under ``--strict`` (exit code 1)."""
    return bool(error_count(result) or (strict and warning_count(result)))


def summary(result: Result) -> dict[str, Any]:
    findings = _findings(result)
    by_sev = Counter(f.severity for _, f in findings)
    return {
        "by_severity": {"ERROR": by_sev.get("ERROR", 0), "WARN": by_sev.get("WARN", 0)},
        "by_code": dict(sorted(Counter(f.code for _, f in findings).items())),
    }


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.lower().split("_"))


# --------------------------------------------------------------------------- text
def render_text(result: Result) -> str:
    """Human-readable output (the historical default contract; unchanged)."""
    lines: list[str] = []
    status = "PASS" if result.ok else "FAIL"
    if isinstance(result, CollectionAuditResult):
        collection_state = "FAIL" if result.collection_assets_failed else "PASS"
        lines.append(
            f"{status} {result.collection_id} | collection-assets: {collection_state} | "
            f"items: {result.items_checked} | failing-items: {result.failing_items} | "
            f"assets: {result.checked_assets} | errors: {result.error_count} | warnings: {result.warning_count}"
        )
        if result.codes:
            lines.append("findings: " + ", ".join(f"{k}={v}" for k, v in result.codes.most_common()))
        return "\n".join(lines) + "\n"

    lines.append(
        f"{status} {result.item_id} | checked: {result.checked_assets} | "
        f"skipped: {result.skipped_assets} | errors: {len(result.errors)} | warnings: {len(result.warnings)}"
    )
    for finding in result.findings:
        lines.extend(_finding_lines(finding))
    return "\n".join(lines) + "\n"


def _finding_lines(finding: Finding) -> list[str]:
    asset = f" asset={finding.asset}" if finding.asset else ""
    out = [f"[{finding.severity}] {finding.code}{asset} field={finding.field}: {finding.message}"]
    if finding.declared is not None or finding.actual is not None:
        out.append(f"  declared={finding.declared!r}")
        out.append(f"  actual={finding.actual!r}")
    return out


# --------------------------------------------------------------------------- json
def report_dict(result: Result, *, strict: bool = False, include_items: bool = True) -> dict[str, Any]:
    """``to_dict()`` plus additive, versioned keys: schema_version, status, strict, summary."""
    if isinstance(result, CollectionAuditResult):
        base = result.to_dict(include_items=include_items)
    else:
        base = result.to_dict()
    doc: dict[str, Any] = {"schema_version": SCHEMA_VERSION}
    doc.update(base)
    doc["status"] = "fail" if gate_failed(result, strict) else "pass"
    doc["strict"] = strict
    # Collection results already carry a summary block (counts, sampling
    # completeness); the severity/code breakdown is added to it, not over it.
    doc["summary"] = {**(base.get("summary") or {}), **summary(result)}
    return doc


def render_json(result: Result, *, strict: bool = False, include_items: bool = True) -> str:
    return json.dumps(report_dict(result, strict=strict, include_items=include_items), indent=2, default=str) + "\n"


# --------------------------------------------------------------------------- sarif
def _is_url(source: str) -> bool:
    return urlparse(source).scheme in {"http", "https", "s3", "gs", "file"}


def _artifact_uri(source: str) -> str:
    """URLs stay as-is; local paths become repo-relative (POSIX) when under CWD,
    which is what GitHub code scanning needs to annotate the file."""
    if _is_url(source):
        return source
    path = Path(source).resolve()
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_uri()


def _asset_line(source: str, asset: str, cache: dict[str, list[str] | None]) -> int | None:
    """Best-effort 1-based line of ``"<asset>":`` in a local JSON file."""
    if _is_url(source) or not asset:
        return None
    if source not in cache:
        try:
            cache[source] = Path(source).read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            cache[source] = None
    lines = cache[source]
    if not lines:
        return None
    key = asset.split(",")[0]
    pattern = re.compile(r'"' + re.escape(key) + r'"\s*:')
    # Search from the "assets" key onward so a same-named key earlier in the document is not hit.
    assets_re = re.compile(r'"assets"\s*:')
    for i, line in enumerate(lines):
        m = assets_re.search(line)
        if m:
            if pattern.search(line, m.end()):
                return i + 1
            for j in range(i + 1, len(lines)):
                if pattern.search(lines[j]):
                    return j + 1
            return None
    return None


def _fingerprint(r: AuditResult, f: Finding) -> str:
    raw = "\x1f".join([r.item_id, f.asset, f.field, f.code])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def sarif_dict(result: Result, *, strict: bool = False) -> dict[str, Any]:
    findings = _findings(result)
    rule_ids: list[str] = sorted(set(RULES) | {f.code for _, f in findings})
    first_sev = {}
    for _, f in findings:
        first_sev.setdefault(f.code, f.severity)
    rules = []
    for code in rule_ids:
        sev, text = RULES.get(code, (first_sev.get(code, "WARN"), f"stac-integrity finding {code}."))
        rules.append(
            {
                "id": code,
                "name": _camel(code),
                "shortDescription": {"text": text},
                "defaultConfiguration": {"level": _SARIF_LEVEL.get(sev, "warning")},
                "properties": {
                    "tags": ["stac", "raster", "semantic-integrity" if sev == "ERROR" else "insufficient-evidence"],
                },
            }
        )
    index = {code: i for i, code in enumerate(rule_ids)}

    sarif_results = []
    line_cache: dict[str, list[str] | None] = {}
    for r, f in findings:
        location: dict[str, Any] = {"artifactLocation": {"uri": _artifact_uri(r.source)}}
        line = _asset_line(r.source, f.asset, line_cache)
        if line:
            location["region"] = {"startLine": line}
        fqn = f"{r.item_id}/assets/{f.asset}/{f.field}" if f.asset else f"{r.item_id}/{f.field}"
        text = f"{f.message} (item={r.item_id}" + (f", asset={f.asset}" if f.asset else "") + f", field={f.field})"
        sarif_results.append(
            {
                "ruleId": f.code,
                "ruleIndex": index[f.code],
                "level": _SARIF_LEVEL.get(f.severity, "warning"),
                "message": {"text": text},
                "locations": [
                    {
                        "physicalLocation": location,
                        "logicalLocations": [
                            {"name": f.asset or f.field, "fullyQualifiedName": fqn, "kind": "member"}
                        ],
                    }
                ],
                "partialFingerprints": {"stacIntegrityFinding/v1": _fingerprint(r, f)},
                "properties": {
                    "severity": f.severity,
                    "item_id": r.item_id,
                    "asset": f.asset,
                    "field": f.field,
                    "declared": _jsonable(f.declared),
                    "actual": _jsonable(f.actual),
                },
            }
        )

    run_props: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "fail" if gate_failed(result, strict) else "pass",
        "strict": strict,
        "summary": summary(result),
    }
    if isinstance(result, CollectionAuditResult):
        run_props.update(collection_id=result.collection_id, items_checked=result.items_checked)
    else:
        run_props.update(item_id=result.item_id)
    run_props.update(checked_assets=result.checked_assets, skipped_assets=result.skipped_assets)

    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": TOOL_NAME,
                        "version": __version__,
                        "semanticVersion": __version__,
                        "rules": rules,
                    }
                },
                "invocations": [{"executionSuccessful": True}],
                "results": sarif_results,
                "properties": run_props,
            }
        ],
    }


def render_sarif(result: Result, *, strict: bool = False) -> str:
    return json.dumps(sarif_dict(result, strict=strict), indent=2, default=str) + "\n"


# --------------------------------------------------------------------------- junit
def _testcase(r: AuditResult, classname: str, strict: bool) -> ET.Element:
    """One <testcase> per STAC Item (or per collection-level asset set).

    - any ERROR                      -> <failure type=first ERROR code>
    - WARN only, --strict            -> <failure> (mirrors exit code 1)
    - WARN only, nothing inspected   -> <skipped> (no evidence either way)
    - WARN only otherwise            -> pass; warnings listed in <system-out>
    """
    case = ET.Element("testcase", {"classname": classname, "name": r.item_id, "time": "0"})
    errors, warnings = r.errors, r.warnings
    failing = errors or (warnings if strict else [])
    if failing:
        codes = ", ".join(dict.fromkeys(f.code for f in failing))
        failure = ET.SubElement(
            case,
            "failure",
            {"message": f"{len(failing)} {'ERROR' if errors else 'WARN (--strict)'} finding(s): {codes}", "type": failing[0].code},
        )
        failure.text = "\n".join(line for f in failing for line in _finding_lines(f))
    elif r.checked_assets == 0:
        reason = ", ".join(dict.fromkeys(f.code for f in warnings)) or "no raster assets inspected"
        ET.SubElement(case, "skipped", {"message": f"No raster asset could be inspected ({reason})"})
    if r.findings:
        out = ET.SubElement(case, "system-out")
        out.text = "\n".join(line for f in r.findings for line in _finding_lines(f))
    return case


def junit_element(result: Result, *, strict: bool = False) -> ET.Element:
    if isinstance(result, CollectionAuditResult):
        name, source = result.collection_id, result.source
        classname = f"stac-integrity.collection.{result.collection_id}"
    else:
        name, source = result.item_id, result.source
        classname = "stac-integrity.item"
    cases = [_testcase(r, classname, strict) for r in _results(result)]
    failures = sum(1 for c in cases if c.find("failure") is not None)
    skipped = sum(1 for c in cases if c.find("skipped") is not None)
    counts = {"tests": str(len(cases)), "failures": str(failures), "errors": "0", "skipped": str(skipped), "time": "0"}

    root = ET.Element("testsuites", {"name": TOOL_NAME, **counts})
    suite = ET.SubElement(root, "testsuite", {"name": name, **counts})
    props = ET.SubElement(suite, "properties")
    s = summary(result)
    for key, value in (
        ("schema_version", SCHEMA_VERSION),
        ("tool_version", __version__),
        ("source", source),
        ("strict", str(strict).lower()),
        ("status", "fail" if gate_failed(result, strict) else "pass"),
        ("error_count", s["by_severity"]["ERROR"]),
        ("warning_count", s["by_severity"]["WARN"]),
        ("checked_assets", result.checked_assets),
        ("skipped_assets", result.skipped_assets),
    ):
        ET.SubElement(props, "property", {"name": key, "value": str(value)})
    suite.extend(cases)
    return root


def render_junit(result: Result, *, strict: bool = False) -> str:
    root = junit_element(result, strict=strict)
    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


# --------------------------------------------------------------------------- dispatch
def render(result: Result, fmt: str, *, strict: bool = False, include_items: bool = True) -> str:
    if fmt == "text":
        return render_text(result)
    if fmt == "json":
        return render_json(result, strict=strict, include_items=include_items)
    if fmt == "sarif":
        return render_sarif(result, strict=strict)
    if fmt == "junit":
        return render_junit(result, strict=strict)
    raise ValueError(f"unknown format: {fmt}")


def write_report(text: str, path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
