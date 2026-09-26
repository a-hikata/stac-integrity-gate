#!/usr/bin/env python3
"""Wave 2: NASA GHG Center / VEDA public STAC as additional false-positive controls.

Collections are chosen mechanically: all collections listed, shuffled with a
fixed seed, the first N that expose a GeoTIFF/COG asset on their first Items.
For each Item: stac-integrity-gate (all data-role raster assets) plus the
independent raw TIFF parser and `rio info` on every audited asset.
Output: benchmark/results/wave2/nasa-<name>.json
"""
from __future__ import annotations

import json
import random
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from stac_integrity.audit import _is_data_asset, _is_raster_asset, audit_item_dict  # noqa: E402
from verify_tiff_header import parse  # noqa: E402
from wave2_pc import rio_info  # noqa: E402

OUT = HERE / "results" / "wave2"
SEED = 20260925
ENDPOINTS = {
    "ghgcenter": "https://earth.gov/ghgcenter/api/stac/",
    "veda": "https://openveda.cloud/api/stac/",
}
N_COLLECTIONS = 6
N_ITEMS = 2


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "stac-integrity-gate-wave2"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def next_link(doc: dict) -> str | None:
    return next((l["href"] for l in doc.get("links", []) if l.get("rel") == "next"), None)


def all_collections(root: str) -> list[str]:
    ids, url = [], urljoin(root, "collections")
    while url:
        doc = fetch(url)
        ids += [c["id"] for c in doc.get("collections", [])]
        url = next_link(doc)
    return sorted(ids)


def run(name: str, root: str) -> dict:
    ids = all_collections(root)
    rng = random.Random(SEED)
    order = ids[:]
    rng.shuffle(order)
    chosen, rows, skipped = [], [], []
    for cid in order:
        if len(chosen) >= N_COLLECTIONS:
            break
        try:
            items = fetch(urljoin(root, f"collections/{cid}/items?limit={N_ITEMS}")).get("features", [])
        except Exception as exc:  # noqa: BLE001
            skipped.append({"collection": cid, "reason": f"items fetch failed: {exc}"})
            continue
        cog_items = [it for it in items if any(
            isinstance(a, dict) and a.get("href") and _is_raster_asset(a, a["href"]) and _is_data_asset(a)
            for a in it.get("assets", {}).values())]
        if not cog_items:
            skipped.append({"collection": cid, "reason": "no data-role GeoTIFF/COG assets on first Items"})
            continue
        chosen.append(cid)
        for it in cog_items[:N_ITEMS]:
            src = urljoin(root, f"collections/{cid}/items/{it['id']}")
            tool = audit_item_dict(it, source=src).to_dict()
            assets = {}
            for key, a in it["assets"].items():
                if not (isinstance(a, dict) and a.get("href") and _is_raster_asset(a, a["href"]) and _is_data_asset(a)):
                    continue
                try:
                    hdr = parse(a["href"])
                    hdr.pop("gdal_metadata", None)
                except Exception as exc:  # noqa: BLE001
                    hdr = {"error": f"{type(exc).__name__}: {exc}"}
                assets[key] = {
                    "href": a["href"],
                    "declared": {k: a.get(k, it["properties"].get(k)) for k in
                                 ("proj:epsg", "proj:code", "proj:shape", "proj:transform", "proj:bbox")},
                    "declared_bands": a.get("bands") or a.get("raster:bands"),
                    "independent_tiff_parser": hdr,
                    "rio_info": rio_info(a["href"]),
                }
            rows.append({"collection": cid, "item": it["id"], "tool": tool, "assets": assets})
    out = {"endpoint": root, "total_collections": len(ids), "selection_seed": SEED,
           "chosen": chosen, "skipped_before_quota": skipped, "rows": rows}
    (OUT / f"nasa-{name}.json").write_text(json.dumps(out, indent=2, default=str))
    return out


def main() -> int:
    for name, root in ENDPOINTS.items():
        out = run(name, root)
        print(f"== {name}: {out['total_collections']} collections, chosen {out['chosen']}, skipped {len(out['skipped_before_quota'])}")
        for r in out["rows"]:
            t = r["tool"]
            codes = sorted({f"{f['severity']}:{f['code']}" for f in t["findings"]})
            print(f"   {r['collection'][:40]:40} {r['item'][:40]:40} checked={t['checked_assets']} {codes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
