#!/usr/bin/env python3
"""Wave 2: Microsoft Planetary Computer 3dep-seamless live validation.

- Sample is mechanical: fixed seed, spread over distinct threedep:region values.
- Item metadata is the *raw* STAC API JSON (no pystac migration). Only asset
  hrefs are replaced with SAS-signed URLs (planetary_computer.sign), in a copy.
- For each Item: run stac-integrity-gate (audit_item_dict) and two independent
  header reads: (1) `rio info` from a separate rasterio install (subprocess),
  (2) benchmark/verify_tiff_header.py (pure-Python TIFF parse, no GDAL).
Output: benchmark/results/wave2/pc-3dep-results.json
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import random
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import planetary_computer as pc

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
from stac_integrity.audit import audit_item_dict  # noqa: E402
from verify_tiff_header import parse  # noqa: E402

API = "https://planetarycomputer.microsoft.com/api/stac/v1/collections/3dep-seamless/items/"
OUT = HERE / "results" / "wave2"
# Point RIO_BIN at a rasterio install separate from the tool's own environment.
RIO = os.environ.get("RIO_BIN") or shutil.which("rio") or "rio"
SEED = 20260925
KNOWN_BAD = ["n31w105-13", "n34w112-13", "n40w079-13", "n40w080-13", "n41w080-13", "n41w079-13"]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "stac-integrity-gate-wave2"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def pick(pool: list[dict], n: int, rng: random.Random, exclude: set[str]) -> list[dict]:
    pool = [p for p in pool if p["id"] not in exclude]
    rng.shuffle(pool)
    chosen, regions = [], set()
    for p in pool:  # first pass: one per region
        if len(chosen) < n and p["region"] not in regions:
            chosen.append(p)
            regions.add(p["region"])
    for p in pool:  # fill
        if len(chosen) < n and p not in chosen:
            chosen.append(p)
    return chosen


def sample() -> list[tuple[str, str]]:
    meta = json.loads((OUT / "pc-3dep-catalog-metadata.json").read_text())
    rng = random.Random(SEED)
    y = lambda m: (m["datetime"] or "")[:4]  # noqa: E731
    used = set(KNOWN_BAD)
    plan = [("known-bad", i) for i in KNOWN_BAD]
    groups = [
        ("10m-2013", [m for m in meta if m["gsd"] == 10 and y(m) == "2013"], 24),
        ("10m-other", [m for m in meta if m["gsd"] == 10 and y(m) != "2013"], 12),
        ("30m-2013", [m for m in meta if m["gsd"] == 30 and y(m) == "2013"], 6),
        ("30m-other", [m for m in meta if m["gsd"] == 30 and y(m) != "2013"], 8),
    ]
    for name, pool, n in groups:
        for m in pick(pool, n, rng, used):
            used.add(m["id"])
            plan.append((name, m["id"]))
    return plan


def rio_info(href: str) -> dict:
    p = subprocess.run([RIO, "info", href], capture_output=True, text=True, timeout=180)
    if p.returncode != 0:
        return {"error": p.stderr.strip()[-300:]}
    d = json.loads(p.stdout)
    return {"crs": d.get("crs"), "shape_hw": d.get("shape"), "transform": d.get("transform")[:6],
            "count": d.get("count"), "dtype": d.get("dtype"), "nodata": d.get("nodata")}


def run_one(group_id: tuple[str, str]) -> dict:
    group, item_id = group_id
    raw = fetch(API + item_id)
    signed = copy.deepcopy(raw)
    for a in signed.get("assets", {}).values():  # only hrefs change
        if "href" in a:
            a["href"] = pc.sign(a["href"])
    tool = audit_item_dict(signed, source=API + item_id, asset_keys=["data"]).to_dict()
    href = signed["assets"]["data"]["href"]
    props = raw["properties"]
    declared = {k: props.get(k) for k in ("proj:epsg", "proj:code", "proj:shape", "proj:transform", "proj:bbox")}
    declared.update({k: raw["assets"]["data"].get(k) for k in ("proj:epsg", "proj:shape", "proj:transform") if k in raw["assets"]["data"]})
    declared["bands_declared"] = "raster:bands" in raw["assets"]["data"] or "bands" in raw["assets"]["data"]
    try:
        hdr = parse(href)
        hdr.pop("href", None)
        hdr.pop("gdal_metadata", None)
    except Exception as exc:  # noqa: BLE001
        hdr = {"error": f"{type(exc).__name__}: {exc}"}
    rio = rio_info(href)
    return {
        "group": group, "id": item_id, "gsd": props.get("gsd"), "datetime": props.get("datetime"),
        "region": props.get("threedep:region"), "unsigned_href": raw["assets"]["data"]["href"],
        "declared": declared, "tool": tool, "independent_tiff_parser": hdr, "rio_info": rio,
    }


def main() -> int:
    plan = sample()
    with ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(run_one, plan))
    (OUT / "pc-3dep-results.json").write_text(json.dumps(rows, indent=2, default=str))
    for r in rows:
        codes = sorted(f["code"] for f in r["tool"]["findings"])
        t = r["independent_tiff_parser"].get("transform", [None])[0]
        rt = (r["rio_info"].get("transform") or [None])[0]
        print(f"{r['group']:10} {r['id']:12} gsd={r['gsd']} {r['datetime'][:4]} decl_px={r['declared']['proj:transform'][0]} "
              f"parser_px={t} rio_px={rt} rio_crs={r['rio_info'].get('crs')} tool={codes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
