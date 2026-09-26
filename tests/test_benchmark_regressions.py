"""Offline benchmark manifest vs committed baseline (no network).

The benchmark tooling lives in ``benchmark/`` (dev-only, pruned from the sdist),
so these tests skip when it is absent.
"""
import copy
import json
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "benchmark" / "manifest.json").exists():
    pytest.skip("benchmark/ tooling not present (sdist)", allow_module_level=True)

from benchmark import run as bench  # noqa: E402
from benchmark.fixtures import BUILDERS  # noqa: E402

BASELINE = ROOT / "benchmark" / "baseline" / "offline.json"


@pytest.fixture(scope="module")
def manifest():
    return bench.load_manifest()


@pytest.fixture(scope="module")
def offline_results(manifest):
    return bench.run_suite(bench.select_cases(manifest, "offline", None), mode="offline")


def test_manifest_is_consistent(manifest):
    targets = {t["name"] for t in json.loads(bench.DEFAULT_TARGETS.read_text())["targets"]}
    for case in manifest["cases"]:
        if case["kind"] == "offline-fixture":
            assert case["fixture"]["builder"] in BUILDERS, case["id"]
        elif "target" in case["live"]:
            assert case["live"]["target"] in targets, case["id"]


def test_offline_suite_meets_every_expectation(offline_results):
    failing = [(r["id"], r.get("checks"), r.get("error")) for r in offline_results["cases"] if r["status"] != "PASS"]
    assert not failing


def test_offline_suite_has_no_regression_vs_baseline(offline_results):
    diff = bench.compare(json.loads(BASELINE.read_text()), offline_results)
    assert diff["regressions"] == 0, json.dumps(diff, indent=2)


def test_offline_results_carry_no_local_paths(offline_results):
    text = json.dumps(offline_results)
    assert str(ROOT) not in text and "/tmp" not in text and "/var/folders" not in text


def test_offline_mode_refuses_unexpected_network():
    with bench.offline_network({}):
        with pytest.raises(bench.OfflineViolation):
            urllib.request.urlopen("https://example.org/item.json")


def test_baseline_diff_classifies_changes():
    base = json.loads(BASELINE.read_text())
    cur = copy.deepcopy(base)
    rows = {r["id"]: r for r in cur["cases"]}
    # Control starts emitting an unexpected ERROR -> new false positive.
    ctl = rows["e84-s2-c1-red-control"]
    ctl["findings"].append({"code": "BBOX_MISMATCH", "severity": "ERROR", "asset": "red", "field": "proj:bbox"})
    ctl["checks"]["unexpected"] = [["BBOX_MISMATCH", "ERROR"]]
    ctl["status"], ctl["exit_code"] = "FAIL", 1
    # Known-bad loses its expected ERROR -> new false negative.
    bad = rows["dea-mstp-shape-off-by-one"]
    bad["findings"], bad["checks"]["missing"], bad["status"] = [], [["SHAPE_MISMATCH", "ERROR"]], "FAIL"
    # WARN becomes ERROR -> changed severity.
    sev = rows["pc-3dep-compound-crs"]
    sev["findings"][0]["severity"] = "ERROR"
    # 10x slower -> timing regression (reported, not counted by default).
    rows["nasa-ghg-float-epsg"]["audit_s"] = 5.0

    diff = bench.compare(base, cur)
    assert [d["case"] for d in diff["new_false_positives"]] == ["e84-s2-c1-red-control"]
    assert [d["case"] for d in diff["new_false_negatives"]] == ["dea-mstp-shape-off-by-one"]
    assert [d["code"] for d in diff["changed_severities"]] == ["CRS_VERTICAL_UNVERIFIED"]
    assert [d["case"] for d in diff["timing_regressions"]] == ["nasa-ghg-float-epsg"]
    assert {d["case"] for d in diff["newly_failing"]} == {"e84-s2-c1-red-control", "dea-mstp-shape-off-by-one"}
    assert diff["regressions"] == 6
    assert bench.compare(base, cur, fail_on_timing=True)["regressions"] == 7
    # And the reverse direction reports improvements, not regressions.
    back = bench.compare(cur, base)
    assert len(back["resolved_false_positives"]) == 1 and len(back["resolved_false_negatives"]) == 1


def test_detector_regression_is_caught_end_to_end(manifest, monkeypatch):
    # Simulate a broken CRS check in the package: the DEA nidem known-bad loses
    # its CRS_MISMATCH ERROR and the 3DEP control loses its vertical WARN.
    import stac_integrity.audit as audit

    monkeypatch.setattr(audit, "_check_crs", lambda *a, **k: iter(()))
    ids = ["dea-nidem-crs-nodata", "pc-3dep-compound-crs"]
    results = bench.run_suite(bench.select_cases(manifest, "offline", ids), mode="offline")
    diff = bench.compare(json.loads(BASELINE.read_text()), results, partial=True)
    assert [(d["case"], d["code"]) for d in diff["new_false_negatives"]] == [("dea-nidem-crs-nodata", "CRS_MISMATCH")]
    assert {d["case"] for d in diff["newly_failing"]} == set(ids)
    assert diff["cases_removed"] == []
