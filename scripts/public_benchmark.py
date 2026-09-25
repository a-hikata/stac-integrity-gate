#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from stac_integrity.audit import audit_item
from stac_integrity.collection import audit_collection


def _all_results(result):
    if hasattr(result, "_all_results"):
        return result._all_results()
    return [result]


def _semantic_errors(result) -> int:
    return sum(
        1
        for r in _all_results(result)
        for f in r.findings
        if f.severity == "ERROR" and f.code != "ASSET_UNREADABLE"
    )


def _unreadable(result) -> int:
    return sum(
        1
        for r in _all_results(result)
        for f in r.findings
        if f.code == "ASSET_UNREADABLE"
    )


def _summary(result, include_details: bool):
    if hasattr(result, "to_dict"):
        try:
            return result.to_dict(include_items=include_details)
        except TypeError:
            return result.to_dict()
    raise TypeError("Unsupported audit result")


def run_target(target: dict[str, Any]) -> dict[str, Any]:
    source = target["source"]
    assets = target.get("assets")
    kind = target.get("kind", "collection")
    try:
        if kind == "item":
            result = audit_item(
                source,
                asset_keys=assets,
                unreadable_severity="WARN",
            )
        elif kind == "collection":
            result = audit_collection(
                source,
                limit=int(target.get("limit", 5)),
                workers=int(target.get("workers", 6)),
                asset_keys=assets,
                unreadable_severity="WARN",
            )
        else:
            raise ValueError(f"Unsupported target kind: {kind}")

        unreadable = _unreadable(result)
        checked = sum(r.checked_assets for r in _all_results(result))
        opened = max(0, checked - unreadable)
        semantic = _semantic_errors(result)
        if semantic:
            signal = "MISMATCH_FOUND"
        elif opened:
            signal = "NO_MISMATCH_FOUND"
        else:
            signal = "INCONCLUSIVE"

        expected = target.get("expected_signal")
        return {
            "name": target["name"],
            "kind": kind,
            "source": source,
            "intent": target.get("intent"),
            "signal": signal,
            "expected_signal": expected,
            "expectation_met": None if expected is None else signal == expected,
            "opened_assets": opened,
            "unreadable_assets": unreadable,
            "semantic_errors": semantic,
            "summary": _summary(result, False),
            "details": _summary(result, True),
        }
    except Exception as exc:
        expected = target.get("expected_signal")
        return {
            "name": target["name"],
            "kind": kind,
            "source": source,
            "intent": target.get("intent"),
            "signal": "INCONCLUSIVE",
            "expected_signal": expected,
            "expectation_met": None if expected is None else expected == "INCONCLUSIVE",
            "operational_error": f"{type(exc).__name__}: {exc}",
        }


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Public STAC Integrity Benchmark",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "| Target | Signal | Expected | Opened assets | Semantic errors | Unreadable |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["results"]:
        expected = row.get("expected_signal") or "—"
        if row.get("expected_signal") is not None:
            expected += " ✓" if row.get("expectation_met") else " ✗"
        lines.append(
            f"| {row['name']} | {row['signal']} | {expected} | {row.get('opened_assets', 0)} | "
            f"{row.get('semantic_errors', 0)} | {row.get('unreadable_assets', 0)} |"
        )
    lines += ["", "## Decision rule", ""]
    lines += [
        "- `MISMATCH_FOUND`: at least one readable asset has a semantic ERROR.",
        "- `NO_MISMATCH_FOUND`: assets were opened and no semantic ERROR was found.",
        "- `INCONCLUSIVE`: assets could not be opened or the target did not expose supported raster assets.",
        "",
        "Known-bad targets are regression controls; clean targets are false-positive controls.",
        "Do not treat this small benchmark as a prevalence estimate. It is a cross-provider smoke test.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("config", nargs="?", default="benchmark/targets.json")
    p.add_argument("--out-dir", default="benchmark/results")
    args = p.parse_args()

    config = json.loads(Path(args.config).read_text())
    targets = config.get("targets", [])
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": [run_target(t) for t in targets],
    }

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=2))
    (out / "report.md").write_text(markdown(report))
    print(markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
