import importlib.util
from pathlib import Path

from stac_integrity.audit import AuditResult, Finding

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("public_benchmark", ROOT / "scripts" / "public_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_public_benchmark_defaults_to_unsigned_public_bucket_access():
    # Live regression: cop-dem-glo-30 (a clean control) was INCONCLUSIVE only
    # because GDAL would not attempt unsigned reads of a public s3:// bucket.
    mod = _load()
    env = {}
    mod.configure_anonymous_access(env)
    assert env["AWS_NO_SIGN_REQUEST"] == "YES"


def test_public_benchmark_respects_explicit_signing_setting():
    mod = _load()
    env = {"AWS_NO_SIGN_REQUEST": "NO"}
    mod.configure_anonymous_access(env)
    assert env["AWS_NO_SIGN_REQUEST"] == "NO"


def test_access_failures_are_inconclusive_not_mismatch(monkeypatch):
    mod = _load()
    unreadable = AuditResult(
        source="https://example.org/item.json",
        item_id="x",
        checked_assets=1,
        skipped_assets=0,
        findings=[Finding("ERROR", "ASSET_UNREADABLE", "data", "href", "403", "s3://requester-pays/x.tif")],
    )
    monkeypatch.setattr(mod, "audit_item", lambda *a, **k: unreadable)
    row = mod.run_target({"name": "t", "kind": "item", "source": "https://example.org/item.json",
                          "expected_signal": "NO_MISMATCH_FOUND"})
    assert row["signal"] == "INCONCLUSIVE"
    assert row["semantic_errors"] == 0
