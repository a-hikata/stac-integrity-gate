#!/usr/bin/env python3
"""Per-item agreement: tool verdict vs two independent header reads (rio info, raw TIFF parser)."""
import json
import math
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "results" / "wave2"
rows = json.loads((OUT / sys.argv[1] if len(sys.argv) > 1 else OUT / "pc-3dep-results.json").read_text())


def grid_ok(decl_shape, decl_tr, shape, tr):
    px = abs(tr[0])
    tol = [px * 1e-7, px * 1e-7, px * 0.01, px * 1e-7, px * 1e-7, px * 0.01]
    return (list(decl_shape) == list(shape),
            all(math.isclose(float(d), float(a), abs_tol=t) for d, a, t in zip(decl_tr[:6], tr, tol)))


summary = []
for r in rows:
    d, p, rio = r["declared"], r["independent_tiff_parser"], r["rio_info"]
    s_p, t_p = grid_ok(d["proj:shape"], d["proj:transform"], p["shape_hw"], p["transform"])
    s_r, t_r = grid_ok(d["proj:shape"], d["proj:transform"], rio["shape_hw"], rio["transform"])
    readers_agree = (p["shape_hw"] == rio["shape_hw"]
                     and all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-15) for a, b in zip(p["transform"], rio["transform"]))
                     and f"EPSG:{p.get('epsg_geographic') or p.get('epsg_projected')}" == rio["crs"])
    tool_err = {f["code"] for f in r["tool"]["findings"] if f["severity"] == "ERROR"}
    indep_err = set()
    if not s_p:
        indep_err.add("SHAPE_MISMATCH")
    if not t_p:
        indep_err.add("TRANSFORM_MISMATCH")
    tool_grid = tool_err & {"SHAPE_MISMATCH", "TRANSFORM_MISMATCH"}
    summary.append({
        "id": r["id"], "group": r["group"], "gsd": r["gsd"], "year": r["datetime"][:4],
        "declared_epsg": d.get("proj:epsg"), "declared_shape": d["proj:shape"], "declared_transform": d["proj:transform"][:6],
        "actual_crs": rio["crs"], "actual_shape": p["shape_hw"], "actual_transform": p["transform"],
        "actual_count": rio["count"], "actual_dtype": rio["dtype"], "actual_nodata": rio["nodata"],
        "readers_agree": readers_agree, "tool_errors": sorted(tool_err), "independent_errors": sorted(indep_err),
        "rio_errors_same_as_parser": (s_p, t_p) == (s_r, t_r),
        "tool_matches_independent": tool_grid == indep_err and tool_err <= {"SHAPE_MISMATCH", "TRANSFORM_MISMATCH"},
    })
(OUT / "pc-3dep-agreement.json").write_text(json.dumps(summary, indent=2))
n = len(summary)
print("items", n)
print("readers agree (rio vs raw parser):", sum(s["readers_agree"] for s in summary), "/", n)
print("rio & parser give same verdict:", sum(s["rio_errors_same_as_parser"] for s in summary), "/", n)
print("tool verdict == independent verdict:", sum(s["tool_matches_independent"] for s in summary), "/", n)
print("mismatch items:", sum(1 for s in summary if s["independent_errors"]))
print("clean items:", sum(1 for s in summary if not s["independent_errors"]), " tool false positives:",
      sum(1 for s in summary if not s["independent_errors"] and s["tool_errors"]))
print("tool false negatives:", sum(1 for s in summary if s["independent_errors"] and not s["tool_errors"]))
