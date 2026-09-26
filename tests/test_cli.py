"""CLI contract: exit codes 0 (clean) / 1 (semantic ERROR) / 2 (operational failure)."""
import json
from pathlib import Path

import pytest

from stac_integrity import __version__
from stac_integrity.cli import main
from test_audit import make_fixture

ROOT = Path(__file__).resolve().parents[1]


def test_known_good_item_exits_0(tmp_path, capsys):
    item_path, _ = make_fixture(tmp_path)
    assert main(["item", str(item_path)]) == 0
    assert capsys.readouterr().out.startswith("PASS fixture")


def test_repository_known_bad_item_exits_1(capsys):
    assert main(["item", str(ROOT / "demo_bad" / "item.json")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL known-bad-band-count")
    assert "[ERROR] BAND_COUNT_MISMATCH" in out


def test_repository_known_bad_collection_exits_1(capsys):
    assert main(["collection", str(ROOT / "demo_collection_bad" / "collection.json")]) == 1
    out = capsys.readouterr().out
    assert out.startswith("FAIL known-bad-collection-band-count | collection-assets: FAIL")
    assert "findings: BAND_COUNT_MISMATCH=1" in out


def test_legacy_invocation_without_subcommand(capsys):
    assert main([str(ROOT / "demo_bad" / "item.json")]) == 1


def test_json_output_is_machine_readable(capsys):
    main(["item", str(ROOT / "demo_bad" / "item.json"), "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert doc["ok"] is False
    assert doc["error_count"] == 1
    assert doc["findings"][0]["code"] == "BAND_COUNT_MISMATCH"


def test_warning_only_exits_0_unless_strict(tmp_path):
    item_path, item = make_fixture(tmp_path)
    item["assets"]["data"]["raster:bands"][0]["scale"] = 0.001
    item_path.write_text(json.dumps(item))
    assert main(["item", str(item_path)]) == 0
    assert main(["item", str(item_path), "--strict"]) == 1


def test_missing_input_exits_2(tmp_path, capsys):
    assert main(["item", str(tmp_path / "does-not-exist.json")]) == 2
    assert "stac-integrity:" in capsys.readouterr().err


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"stac-integrity {__version__}"
