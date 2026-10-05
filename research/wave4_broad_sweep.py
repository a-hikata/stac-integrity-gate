#!/usr/bin/env python3
"""Wave 4 blind cross-provider semantic integrity sweep.

Provider selection was fixed before checking their issue trackers.
For each provider: list collections, seeded shuffle, take the first eligible
collections exposing data-role raster assets, then audit the first N Items.
Access failures are INCONCLUSIVE, never semantic mismatches.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from stac_integrity.audit import _is_data_asset, _is_raster_asset, audit_item_dict

SEED = 20261005
UA = "stac-integrity-gate-wave4/0.1 (+blind-public-catalog-audit)"
PROVIDERS = {
    "worldpop": "https://api.stac.worldpop.org",
    "uvt": "https://stac.sage.uvt.ro",
    "thuenen": "https://eodata.thuenen.de/stac/api/v1",
    "eodc": "https://stac.eodc.eu/api/v1",
    "usgs_landsat": "https://landsatlook.usgs.gov/stac-server",
    "cesnet": "https://stac.cesnet.cz",
    "swisstopo_control": "https://data.geo.admin.ch/api/stac/v1",
}

def fetch(url: str, timeout: int = 45) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": UA, "Accept": "application/json, application/geo+json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def next_link(doc: dict[str, Any]) -> str | None:
    for link in doc.get("links", []) or []:
        if link.get("rel") == "next" and link.get("href"):
            return link["href"]
    return None

def list_collections(base: str) -> list[dict[str, Any]]:
    url = base.rstrip("/") + "/collections"
    out: list[dict[str, Any]] = []
    for _ in range(30):
        doc = fetch(url)
        out.extend(doc.get("collections", []) or [])
        url = next_link(doc)
        if not url:
            break
    return out

def list_items(base: str, cid: str, limit: int) -> list[dict[str, Any]]:
    cidq = urllib.parse.quote(cid, safe="")
    url = base.rstrip("/") + f"/collections/{cidq}/items?limit={min(limit, 100)}"
    out: list[dict[str, Any]] = []
    for _ in range(10):
        doc = fetch(url)
        out.extend(doc.get("features", []) or [])
        if len(out) >= limit:
            break
        url = next_link(doc)
        if not url:
            break
    return out[:limit]

def raster_keys(item: dict[str, Any], max_assets: int = 2) -> list[str]:
    keys: list[str] = []
    for key, asset in (item.get("assets") or {}).items():
        if not isinstance(asset, dict) or not asset.get("href"):
            continue
        href = asset["href"]
        if _is_raster_asset(asset, href) and _is_data_asset(asset):
            keys.append(key)
            if len(keys) >= max_assets:
                break
    return keys

def public_item_url(base: str, cid: str, iid: str) -> str:
    return (
        base.rstrip("/")
        + "/collections/"
        + urllib.parse.quote(cid, safe="")
        + "/items/"
        + urllib.parse.quote(iid, safe="")
    )

def run_item(provider: str, base: str, cid: str, item: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    source = public_item_url(base, cid, str(item.get("id")))
    try:
        result = audit_item_dict(
            item,
            source=source,
            asset_keys=keys,
            unreadable_severity="WARN",
        )
        d = result.to_dict()
        findings = d.get("findings", [])
        semantic_errors = [
            f for f in findings
            if f.get("severity") == "ERROR" and f.get("code") != "ASSET_UNREADABLE"
        ]
        return {
            "provider": provider,
            "collection": cid,
            "item": item.get("id"),
            "assets": keys,
            "checked_assets": d.get("checked_assets", 0),
            "semantic_errors": semantic_errors,
            "warnings": [
                f for f in findings
                if f.get("severity") == "WARN"
            ],
        }
    except Exception as exc:
        return {
            "provider": provider,
            "collection": cid,
            "item": item.get("id"),
            "assets": keys,
            "operational_error": f"{type(exc).__name__}: {exc}",
        }

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--collections-per-provider", type=int, default=8)
    p.add_argument("--items-per-collection", type=int, default=5)
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--out-dir", default="research/wave4-results")
    args = p.parse_args()

    os.environ.setdefault("AWS_NO_SIGN_REQUEST", "YES")
    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    rng = random.Random(SEED)
    manifest: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "seed": SEED,
        "method": "seeded collection shuffle; first eligible collections; first N items; max two data-role raster assets per item",
        "providers": {},
    }
    work: list[tuple[str, str, str, dict[str, Any], list[str]]] = []

    for provider, base in PROVIDERS.items():
        state: dict[str, Any] = {"base": base, "selected": [], "skipped": []}
        manifest["providers"][provider] = state
        try:
            cols = list_collections(base)
            state["collection_count"] = len(cols)
            ordered = cols[:]
            rng.shuffle(ordered)
        except Exception as exc:
            state["fatal"] = f"{type(exc).__name__}: {exc}"
            continue

        for c in ordered:
            if len(state["selected"]) >= args.collections_per_provider:
                break
            cid = c.get("id")
            if not cid:
                continue
            try:
                items = list_items(base, cid, args.items_per_collection)
            except Exception as exc:
                state["skipped"].append({"collection": cid, "reason": f"items fetch: {type(exc).__name__}: {exc}"})
                continue

            eligible = []
            for item in items:
                keys = raster_keys(item)
                if keys:
                    eligible.append((item, keys))
            if not eligible:
                state["skipped"].append({"collection": cid, "reason": "no data-role raster asset on sampled items"})
                continue

            state["selected"].append({"collection": cid, "items": len(eligible)})
            for item, keys in eligible:
                work.append((provider, base, cid, item, keys))

    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = [ex.submit(run_item, *job) for job in work]
        for n, fut in enumerate(as_completed(futures), 1):
            rows.append(fut.result())
            if n % 25 == 0:
                print(f"audited {n}/{len(work)}", flush=True)

    semantic = [r for r in rows if r.get("semantic_errors")]
    by_root: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in semantic:
        for f in row["semantic_errors"]:
            key = (row["provider"], row["collection"], f.get("code", "UNKNOWN"))
            g = by_root.setdefault(
                key,
                {
                    "provider": row["provider"],
                    "collection": row["collection"],
                    "code": f.get("code"),
                    "items": 0,
                    "examples": [],
                },
            )
            g["items"] += 1
            if len(g["examples"]) < 5:
                g["examples"].append(
                    {"item": row["item"], "assets": row["assets"], "finding": f}
                )

    roots = sorted(by_root.values(), key=lambda x: (-x["items"], x["provider"], x["collection"], x["code"]))
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "providers_attempted": len(PROVIDERS),
        "providers_reached": sum(1 for x in manifest["providers"].values() if not x.get("fatal")),
        "collections_selected": sum(len(x.get("selected", [])) for x in manifest["providers"].values()),
        "items_audited": len(rows),
        "items_with_semantic_error": len(semantic),
        "unique_root_groups": len(roots),
        "providers_with_semantic_error": sorted({r["provider"] for r in roots}),
        "operational_error_items": sum(1 for r in rows if r.get("operational_error")),
        "root_groups": roots,
    }

    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    (outdir / "rows.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False, default=str))
    (outdir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str))

    lines = [
        "# Wave 4 Blind Cross-Provider Sweep",
        "",
        f"- Providers attempted: **{summary['providers_attempted']}**",
        f"- Providers reached: **{summary['providers_reached']}**",
        f"- Collections selected: **{summary['collections_selected']}**",
        f"- Items audited: **{summary['items_audited']}**",
        f"- Items with semantic ERROR: **{summary['items_with_semantic_error']}**",
        f"- Unique provider × collection × code groups: **{summary['unique_root_groups']}**",
        "",
        "| Provider | Collection | Code | Affected sampled Items |",
        "|---|---|---|---:|",
    ]
    for root in roots:
        lines.append(f"| {root['provider']} | {root['collection']} | {root['code']} | {root['items']} |")
    lines += [
        "",
        "Machine results are candidates until independently reproduced and duplicate-checked. Access failures are not semantic findings.",
    ]
    (outdir / "REPORT.md").write_text("\n".join(lines) + "\n")
    print(json.dumps({k: v for k, v in summary.items() if k != "root_groups"}, indent=2))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
