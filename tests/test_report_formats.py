"""Output-format contract: json (schema_version/status), sarif 2.1.0, junit xml, --output."""
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from stac_integrity.cli import main
from stac_integrity.report import RULES, SCHEMA_VERSION
from test_audit import make_fixture

ROOT = Path(__file__).resolve().parents[1]
BAD_ITEM = str(ROOT / "demo_bad" / "item.json")
BAD_COLLECTION = str(ROOT / "demo_collection_bad" / "collection.json")
REPORT_SCHEMA = ROOT / "docs" / "report.schema.json"


def _run(capsys, argv):
    code = main(argv)
    return code, capsys.readouterr().out


def _warn_only_item(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["raster:bands"][0]["scale"] = 0.001
    item_path.write_text(json.dumps(item))
    return str(item_path)


def _mixed_collection(tmp_path):
    """Static collection: one good item, one bad (band count), one with nothing inspectable."""
    good_path, good = make_fixture(tmp_path)
    bad = json.loads(json.dumps(good))
    bad["id"] = "bad"
    bad["assets"]["data"]["raster:bands"] = [{"data_type": "uint16", "nodata": 0}]
    (tmp_path / "bad.json").write_text(json.dumps(bad))
    empty = {**good, "id": "empty", "assets": {"meta": {"href": "meta.xml", "roles": ["metadata"]}}}
    (tmp_path / "empty.json").write_text(json.dumps(empty))
    collection = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": "mixed",
        "description": "d",
        "license": "proprietary",
        "extent": {},
        "links": [
            {"rel": "item", "href": "item.json"},
            {"rel": "item", "href": "bad.json"},
            {"rel": "item", "href": "empty.json"},
        ],
    }
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(collection))
    return str(path)


# ----------------------------------------------------------------------------- json
def test_json_is_additive_and_versioned(capsys):
    code, out = _run(capsys, ["item", BAD_ITEM, "--format", "json"])
    doc = json.loads(out)
    assert code == 1
    # historical keys unchanged
    for key in ("source", "item_id", "checked_assets", "skipped_assets", "ok", "error_count", "warning_count", "findings"):
        assert key in doc
    assert set(doc["findings"][0]) == {"severity", "code", "asset", "field", "message", "declared", "actual"}
    # additive keys
    assert doc["schema_version"] == SCHEMA_VERSION
    assert doc["status"] == "fail"
    assert doc["strict"] is False
    assert doc["summary"] == {"by_severity": {"ERROR": 1, "WARN": 0}, "by_code": {"BAND_COUNT_MISMATCH": 1}}


def test_json_flag_is_alias_of_format_json(capsys):
    _, a = _run(capsys, ["item", BAD_ITEM, "--json"])
    _, b = _run(capsys, ["item", BAD_ITEM, "--format", "json"])
    assert json.loads(a) == json.loads(b)


def test_json_status_follows_strict_but_ok_does_not(tmp_path, capsys):
    item = _warn_only_item(tmp_path)
    code, out = _run(capsys, ["item", item, "--json"])
    doc = json.loads(out)
    assert code == 0 and doc["ok"] is True and doc["status"] == "pass"
    code, out = _run(capsys, ["item", item, "--json", "--strict"])
    doc = json.loads(out)
    assert code == 1 and doc["ok"] is True and doc["status"] == "fail" and doc["strict"] is True


def test_collection_json_keys_and_summary_only(capsys):
    code, out = _run(capsys, ["collection", BAD_COLLECTION, "--format", "json", "--summary-only"])
    doc = json.loads(out)
    assert code == 1
    for key in (
        "source", "collection_id", "items_checked", "checked_assets", "skipped_assets", "ok",
        "collection_assets_failed", "failing_items", "error_count", "warning_count", "finding_codes",
    ):
        assert key in doc
    assert "items" not in doc
    assert doc["schema_version"] == SCHEMA_VERSION and doc["status"] == "fail"
    assert doc["summary"]["by_code"] == {"BAND_COUNT_MISMATCH": 1}


def test_shipped_json_schema_required_keys_are_emitted(tmp_path, capsys):
    schema = json.loads(REPORT_SCHEMA.read_text())
    item_def, coll_def = schema["$defs"]["itemReport"], schema["$defs"]["collectionReport"]
    _, out = _run(capsys, ["item", BAD_ITEM, "--json"])
    assert set(item_def["required"]) <= set(json.loads(out))
    _, out = _run(capsys, ["collection", _mixed_collection(tmp_path), "--json"])
    doc = json.loads(out)
    assert set(coll_def["required"]) <= set(doc)
    bad = next(i for i in doc["items"] if i["item_id"] == "bad")
    assert set(item_def["required"]) <= set(bad)
    assert set(schema["$defs"]["finding"]["required"]) <= set(bad["findings"][0])


# ----------------------------------------------------------------------------- sarif
def _assert_sarif_essentials(doc):
    assert doc["version"] == "2.1.0"
    assert "sarif-2.1.0" in doc["$schema"]
    assert len(doc["runs"]) == 1
    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "stac-integrity-gate" and driver["version"]
    rule_ids = [r["id"] for r in driver["rules"]]
    assert len(rule_ids) == len(set(rule_ids))
    for rule in driver["rules"]:
        assert rule["shortDescription"]["text"]
        assert rule["defaultConfiguration"]["level"] in {"error", "warning", "note", "none"}
    for res in doc["runs"][0]["results"]:
        assert res["ruleId"] in rule_ids
        assert rule_ids[res["ruleIndex"]] == res["ruleId"]
        assert res["level"] in {"error", "warning"}
        assert res["message"]["text"]
        loc = res["locations"][0]
        assert loc["physicalLocation"]["artifactLocation"]["uri"]
        assert loc["logicalLocations"][0]["fullyQualifiedName"]
        assert "declared" in res["properties"] and "actual" in res["properties"]
    return driver, doc["runs"][0]["results"]


def test_sarif_known_bad_item(capsys, monkeypatch):
    monkeypatch.chdir(ROOT)
    code, out = _run(capsys, ["item", "demo_bad/item.json", "--format", "sarif"])
    assert code == 1
    driver, results = _assert_sarif_essentials(json.loads(out))
    assert set(RULES) <= {r["id"] for r in driver["rules"]}
    assert len(results) == 1
    res = results[0]
    assert res["ruleId"] == "BAND_COUNT_MISMATCH" and res["level"] == "error"
    assert res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == "demo_bad/item.json"
    assert res["locations"][0]["physicalLocation"]["region"]["startLine"] >= 1
    assert res["properties"]["declared"] == 1 and res["properties"]["actual"] == 6
    assert res["properties"]["item_id"] == "known-bad-band-count"
    assert res["partialFingerprints"]


def test_sarif_known_good_item(tmp_path, capsys):
    item_path, _ = make_fixture(tmp_path)
    code, out = _run(capsys, ["item", str(item_path), "--format", "sarif"])
    assert code == 0
    doc = json.loads(out)
    _assert_sarif_essentials(doc)
    assert doc["runs"][0]["results"] == []
    assert doc["runs"][0]["properties"]["status"] == "pass"


def test_sarif_warning_level_and_strict_exit(tmp_path, capsys):
    item = _warn_only_item(tmp_path)
    code, out = _run(capsys, ["item", item, "--format", "sarif"])
    assert code == 0
    _, results = _assert_sarif_essentials(json.loads(out))
    assert {r["level"] for r in results} == {"warning"}
    code, out = _run(capsys, ["item", item, "--format", "sarif", "--strict"])
    assert code == 1
    _, results = _assert_sarif_essentials(json.loads(out))
    assert {r["level"] for r in results} == {"warning"}  # --strict changes the gate, not the level


def test_sarif_collection(tmp_path, capsys):
    code, out = _run(capsys, ["collection", _mixed_collection(tmp_path), "--format", "sarif"])
    assert code == 1
    _, results = _assert_sarif_essentials(json.loads(out))
    by_item = {r["properties"]["item_id"]: r for r in results}
    assert by_item["bad"]["ruleId"] == "BAND_COUNT_MISMATCH"
    assert by_item["bad"]["locations"][0]["physicalLocation"]["artifactLocation"]["uri"].endswith("bad.json")
    assert by_item["empty"]["ruleId"] == "NO_RASTER_ASSETS" and by_item["empty"]["level"] == "warning"


# ----------------------------------------------------------------------------- junit
def _junit(out):
    root = ET.fromstring(out)
    assert root.tag == "testsuites"
    suites = root.findall("testsuite")
    assert len(suites) == 1
    suite = suites[0]
    cases = suite.findall("testcase")
    assert int(suite.get("tests")) == len(cases)
    assert int(suite.get("failures")) == sum(1 for c in cases if c.find("failure") is not None)
    assert int(suite.get("skipped")) == sum(1 for c in cases if c.find("skipped") is not None)
    assert suite.get("errors") == "0"
    for c in cases:
        assert c.get("classname") and c.get("name")
    return suite, {c.get("name"): c for c in cases}


def test_junit_known_bad_item(capsys):
    code, out = _run(capsys, ["item", BAD_ITEM, "--format", "junit"])
    assert code == 1
    assert out.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    suite, cases = _junit(out)
    assert suite.get("name") == "known-bad-band-count"
    failure = cases["known-bad-band-count"].find("failure")
    assert failure.get("type") == "BAND_COUNT_MISMATCH"
    assert "declared=1" in failure.text and "actual=6" in failure.text
    props = {p.get("name"): p.get("value") for p in suite.find("properties")}
    assert props["status"] == "fail" and props["schema_version"] == SCHEMA_VERSION


def test_junit_known_good_item(tmp_path, capsys):
    item_path, _ = make_fixture(tmp_path)
    code, out = _run(capsys, ["item", str(item_path), "--format", "junit"])
    assert code == 0
    suite, cases = _junit(out)
    case = cases["fixture"]
    assert case.find("failure") is None and case.find("skipped") is None
    assert suite.get("failures") == "0"


def test_junit_warnings_pass_unless_strict(tmp_path, capsys):
    item = _warn_only_item(tmp_path)
    code, out = _run(capsys, ["item", item, "--format", "junit"])
    assert code == 0
    _, cases = _junit(out)
    assert cases["fixture"].find("failure") is None
    assert "SCALE_MISMATCH" in cases["fixture"].find("system-out").text
    code, out = _run(capsys, ["item", item, "--format", "junit", "--strict"])
    assert code == 1
    _, cases = _junit(out)
    assert cases["fixture"].find("failure").get("type") == "SCALE_MISMATCH"


def test_junit_collection_testcase_per_item(tmp_path, capsys):
    code, out = _run(capsys, ["collection", _mixed_collection(tmp_path), "--format", "junit"])
    assert code == 1
    suite, cases = _junit(out)
    assert suite.get("name") == "mixed"
    assert set(cases) == {"fixture", "bad", "empty"}
    assert cases["fixture"].find("failure") is None
    assert cases["bad"].find("failure").get("type") == "BAND_COUNT_MISMATCH"
    assert cases["empty"].find("skipped") is not None
    assert suite.get("failures") == "1" and suite.get("skipped") == "1"


def test_junit_collection_level_assets(capsys):
    code, out = _run(capsys, ["collection", BAD_COLLECTION, "--format", "junit"])
    assert code == 1
    _, cases = _junit(out)
    assert cases["collection:known-bad-collection-band-count"].find("failure") is not None


# ----------------------------------------------------------------------------- --output / exit codes
@pytest.mark.parametrize("fmt", ["text", "json", "sarif", "junit"])
def test_exit_codes_identical_across_formats(fmt, tmp_path, capsys):
    good, _ = make_fixture(tmp_path)
    assert main(["item", BAD_ITEM, "--format", fmt]) == 1
    assert main(["item", str(good), "--format", fmt]) == 0
    assert main(["collection", BAD_COLLECTION, "--format", fmt]) == 1
    assert main(["item", str(tmp_path / "missing.json"), "--format", fmt]) == 2
    capsys.readouterr()


@pytest.mark.parametrize("fmt", ["json", "sarif", "junit"])
def test_output_writes_report_and_prints_text_summary(fmt, tmp_path, capsys):
    target = tmp_path / "reports" / f"report.{fmt}"
    code, out = _run(capsys, ["item", BAD_ITEM, "--format", fmt, "--output", str(target)])
    assert code == 1
    assert out.startswith("FAIL known-bad-band-count")
    text = target.read_text()
    if fmt == "junit":
        ET.fromstring(text)
    else:
        json.loads(text)


def test_output_not_written_on_operational_failure(tmp_path, capsys):
    target = tmp_path / "r.sarif"
    assert main(["item", str(tmp_path / "missing.json"), "--format", "sarif", "--output", str(target)]) == 2
    assert not target.exists()


def test_unwritable_output_exits_2(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert main(["item", BAD_ITEM, "--format", "sarif", "--output", str(blocker / "r.sarif")]) == 2
    assert "cannot write report" in capsys.readouterr().err


def test_default_text_output_unchanged(capsys):
    code, out = _run(capsys, ["item", BAD_ITEM])
    assert code == 1
    assert out.splitlines()[0] == "FAIL known-bad-band-count | checked: 1 | skipped: 0 | errors: 1 | warnings: 0"
    code, out = _run(capsys, ["item", BAD_ITEM, "--format", "text"])
    assert out.splitlines()[0].startswith("FAIL known-bad-band-count")
