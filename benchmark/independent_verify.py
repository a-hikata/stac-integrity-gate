#!/usr/bin/env python3
"""Cross-check STAC declarations against an independent (non-GDAL) TIFF header read.

Reads the Item sources recorded in benchmark/results/<target>.json files,
re-fetches each Item, and compares declared proj/bands fields with
benchmark/verify_tiff_header.py output. Writes
benchmark/results/independent_verification.json.
"""
from __future__ import annotations

import json
import math
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from verify_tiff_header import parse  # noqa: E402

RESULTS = HERE / "results"

TARGETS = [
    ("earth-search-legacy-aot-known-bad.json", "aot"),
    ("earth-search-aot.json", "aot"),
    ("earth-search-red-control.json", "red"),
    ("cop-dem-control.json", "data"),
    ("nrp-rap-pfg.json", None),
]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "independent-verify/1"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def eff(parent: dict, asset: dict, field: str):
    return asset[field] if field in asset else (parent.get("properties") or {}).get(field)


def close(a, b, tol):
    return all(math.isclose(float(x), float(y), abs_tol=tol) for x, y in zip(a, b))


def compare(parent: dict, asset: dict, hdr: dict) -> dict:
    out: dict = {}
    shape = eff(parent, asset, "proj:shape")
    if shape is not None:
        out["proj:shape"] = {"declared": shape, "header": hdr["shape_hw"], "match": list(shape) == hdr["shape_hw"]}
    tr = eff(parent, asset, "proj:transform")
    if tr is not None and "transform" in hdr:
        px = abs(hdr["transform"][0])
        out["proj:transform"] = {
            "declared": tr[:6],
            "header": hdr["transform"],
            "match": close(tr[:6], hdr["transform"], px * 0.01),
        }
    code = eff(parent, asset, "proj:code") or (
        f"EPSG:{eff(parent, asset, 'proj:epsg')}" if eff(parent, asset, "proj:epsg") else None
    )
    epsg = hdr.get("epsg_projected") or hdr.get("epsg_geographic")
    if code is not None:
        out["crs"] = {"declared": code, "header": f"EPSG:{epsg}", "match": str(code).upper() == f"EPSG:{epsg}"}
    bands = asset.get("bands") if isinstance(asset.get("bands"), list) else asset.get("raster:bands")
    if isinstance(bands, list):
        out["band_count"] = {"declared": len(bands), "header": hdr["band_count"], "match": len(bands) == hdr["band_count"]}
        dt = bands[0].get("data_type") if bands and isinstance(bands[0], dict) else None
        if dt:
            out["data_type"] = {"declared": dt, "header": hdr["dtype"], "match": dt == hdr["dtype"]}
        if bands and isinstance(bands[0], dict) and "nodata" in bands[0]:
            nd = bands[0]["nodata"]
            h = hdr.get("nodata")
            try:
                m = h is not None and float(nd) == float(h)
            except (TypeError, ValueError):
                m = str(nd) == str(h)
            out["nodata"] = {"declared": nd, "header": h, "match": m}
    return out


def main() -> int:
    rows = []
    for fname, asset_key in TARGETS:
        doc = json.loads((RESULTS / fname).read_text())
        if "items" in doc or "collection_assets" in doc:
            sources = [(r["source"], r["item_id"]) for r in doc.get("items", [])]
            if doc.get("collection_assets"):
                sources.insert(0, (doc["source"], doc["collection_assets"]["item_id"]))
        else:
            sources = [(doc["source"], doc["item_id"])]
        for src, item_id in sources:
            parent = fetch(src)
            if parent.get("type") == "Collection":
                assets = parent.get("assets", {})
                parent = {"properties": parent}
            else:
                assets = parent.get("assets", {})
            keys = [asset_key] if asset_key else [
                k for k, a in assets.items()
                if "tiff" in str(a.get("type", "")).lower() and "data" in (a.get("roles") or ["data"])
            ]
            for key in keys:
                asset = assets[key]
                try:
                    hdr = parse(asset["href"])
                except Exception as exc:  # noqa: BLE001
                    rows.append({"target": fname, "item": item_id, "asset": key, "href": asset["href"],
                                 "error": f"{type(exc).__name__}: {exc}"})
                    continue
                cmp = compare(parent, asset, hdr)
                rows.append({
                    "target": fname,
                    "item": item_id,
                    "asset": key,
                    "href": asset["href"],
                    "header": hdr,
                    "comparison": cmp,
                    "all_match": all(v["match"] for v in cmp.values()),
                })
    (RESULTS / "independent_verification.json").write_text(json.dumps(rows, indent=2))
    for r in rows:
        if "error" in r:
            print(f"{r['target']:42} {r['item'][:48]:48} {r['asset']:6} ERROR {r['error'][:80]}")
            continue
        bad = {k: (v["declared"], v["header"]) for k, v in r["comparison"].items() if not v["match"]}
        print(f"{r['target']:42} {r['item'][:48]:48} {r['asset']:6} {'MATCH' if r['all_match'] else 'MISMATCH'} "
              f"checked={sorted(r['comparison'])} {bad if bad else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
