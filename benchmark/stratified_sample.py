#!/usr/bin/env python3
"""Small time-stratified sample via STAC API /search (not a prevalence estimate).

For each (collection, asset, year window) take N Items from /search, run the
tool (audit_item_dict) and the independent non-GDAL header reader on each,
and record both. Output: benchmark/results/stratified-<name>.json
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from stac_integrity.audit import audit_item_dict  # noqa: E402
from independent_verify import compare  # noqa: E402
from verify_tiff_header import parse  # noqa: E402

API = "https://earth-search.aws.element84.com/v1/search"


def search(collection: str, datetime: str, limit: int) -> list[dict]:
    body = json.dumps({"collections": [collection], "datetime": datetime, "limit": limit}).encode()
    req = urllib.request.Request(API, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": "stac-integrity-gate/0.2"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["features"]


def run(name: str, collection: str, asset: str, windows: list[str], per_window: int) -> dict:
    rows = []
    for window in windows:
        for item in search(collection, window, per_window):
            self_href = next((l["href"] for l in item.get("links", []) if l.get("rel") == "self"), API)
            tool = audit_item_dict(item, source=self_href, asset_keys=[asset]).to_dict()
            row = {"window": window, "item": item["id"], "tool": tool}
            if asset in item.get("assets", {}):
                try:
                    hdr = parse(item["assets"][asset]["href"])
                    cmp = compare(item, item["assets"][asset], hdr)
                    row["independent"] = {"comparison": cmp, "all_match": all(v["match"] for v in cmp.values())}
                except Exception as exc:  # noqa: BLE001
                    row["independent"] = {"error": f"{type(exc).__name__}: {exc}"}
            rows.append(row)
    out = {"name": name, "collection": collection, "asset": asset, "windows": windows, "rows": rows}
    (HERE / "results" / f"stratified-{name}.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def main() -> int:
    years = [f"{y}-07-01T00:00:00Z/{y}-07-10T23:59:59Z" for y in range(2018, 2027)]
    plans = [
        ("legacy-s2-l2a-aot", "sentinel-2-l2a", "aot"),
        ("c1-s2-l2a-aot", "sentinel-2-c1-l2a", "aot"),
    ]
    for name, coll, asset in plans:
        out = run(name, coll, asset, years, 2)
        for r in out["rows"]:
            codes = sorted({f["code"] for f in r["tool"]["findings"] if f["severity"] == "ERROR"})
            ind = r.get("independent", {})
            ind_s = ind.get("error") or ("MATCH" if ind.get("all_match") else
                                         {k: (v["declared"], v["header"]) for k, v in ind.get("comparison", {}).items() if not v["match"]})
            print(f"{name:20} {r['window'][:4]} {r['item'][:34]:34} tool={codes or 'OK'} independent={ind_s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
