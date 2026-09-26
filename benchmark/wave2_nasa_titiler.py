#!/usr/bin/env python3
"""Supplementary: NASA assets are not anonymously readable (S3 AccessDenied), so
compare STAC declarations with the publisher's own public titiler /cog/info.
Single server-side method; NOT a stac-integrity-gate run. Output:
benchmark/results/wave2/nasa-titiler-<name>.json
"""
import json
import math
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "wave2"
RASTER = {"ghgcenter": "https://earth.gov/ghgcenter/api/raster", "veda": "https://openveda.cloud/api/raster"}


def info(base, href):
    url = f"{base}/cog/info?url={urllib.parse.quote(href, safe='')}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "stac-integrity-gate-wave2"}), timeout=120) as r:
        return json.load(r)


def cmp(decl, bands, i):
    out = {}
    w, h = i["width"], i["height"]
    l, b, r, t = i["bounds"]
    tr = [(r - l) / w, 0.0, l, 0.0, -(t - b) / h, t]
    epsg = i["crs"].rsplit("/", 1)[-1]
    if decl.get("proj:shape") is not None:
        out["proj:shape"] = (decl["proj:shape"], [h, w], list(decl["proj:shape"]) == [h, w])
    if decl.get("proj:transform") is not None:
        px = abs(tr[0])
        tol = [px * 1e-6, px * 1e-6, px * 0.01, px * 1e-6, px * 1e-6, px * 0.01]
        out["proj:transform"] = (decl["proj:transform"][:6], tr,
                                 all(math.isclose(float(d), a, abs_tol=tt) for d, a, tt in zip(decl["proj:transform"][:6], tr, tol)))
    ep = decl.get("proj:epsg")
    if isinstance(ep, float) and ep.is_integer():
        ep = int(ep)  # GHG Center publishes 4326.0
    code = decl.get("proj:code") or (f"EPSG:{ep}" if ep else None)
    if code:
        out["crs"] = (code, f"EPSG:{epsg}", str(code).upper() == f"EPSG:{epsg}")
    if isinstance(bands, list):
        out["band_count"] = (len(bands), i["count"], len(bands) == i["count"])
        b0 = bands[0] if bands and isinstance(bands[0], dict) else {}
        if b0.get("data_type"):
            out["data_type"] = (b0["data_type"], i["dtype"], b0["data_type"] == i["dtype"])
        if "nodata" in b0:
            nd, act = b0["nodata"], i.get("nodata_value")
            if act is None and i.get("nodata_type") == "Nodata":
                act = "nan"  # titiler serialises a NaN nodata value as JSON null
            try:
                same = (math.isnan(float(nd)) and math.isnan(float(act))) or float(nd) == float(act)
            except (TypeError, ValueError):
                same = nd == act
            out["nodata"] = (nd, act, same)
    return out


for name, base in RASTER.items():
    d = json.loads((OUT / f"nasa-{name}.json").read_text())
    rows = []
    for row in d["rows"]:
        for key, a in row["assets"].items():
            try:
                i = info(base, a["href"])
                c = cmp(a["declared"], a["declared_bands"], i)
                rows.append({"collection": row["collection"], "item": row["item"], "asset": key, "titiler": i, "comparison": c,
                             "mismatches": {k: v for k, v in c.items() if not v[2]}})
            except Exception as exc:  # noqa: BLE001
                rows.append({"collection": row["collection"], "item": row["item"], "asset": key, "error": f"{type(exc).__name__}: {exc}"})
    (OUT / f"nasa-titiler-{name}.json").write_text(json.dumps(rows, indent=2, default=str))
    ok = [r for r in rows if "error" not in r]
    print(f"== {name}: assets {len(rows)}, read {len(ok)}, errors {len(rows) - len(ok)}, "
          f"with any declared field compared {sum(1 for r in ok if r['comparison'])}, mismatching {sum(1 for r in ok if r['mismatches'])}")
    for r in ok:
        if r["mismatches"]:
            print("   MISMATCH", r["collection"], r["item"], r["asset"], r["mismatches"])
    fields = {}
    for r in ok:
        for k in r["comparison"]:
            fields[k] = fields.get(k, 0) + 1
    print("   fields compared:", fields)
    for r in rows:
        if "error" in r:
            print("   ERR", r["collection"], r["asset"], r["error"][:150])
