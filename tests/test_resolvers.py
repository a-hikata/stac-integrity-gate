"""Track B: href resolvers (signed/authenticated assets) and credential redaction.

Fully offline: signed URLs are redirected to local GeoTIFFs by resolvers, and
remote access failures are simulated by patching ``rasterio.open``.
"""
import json
import sys
import types
from pathlib import Path

import pytest
import rasterio
from rasterio.errors import RasterioIOError

from stac_integrity import HrefContext, audit_collection, audit_item
from stac_integrity.audit import audit_item_dict
from stac_integrity.cli import main
from stac_integrity.redaction import REDACTED, redact
from stac_integrity.resolvers import (
    ResolverUnavailableError,
    alternate_resolver,
    identity,
    load_resolver,
    planetary_computer_resolver,
)
from test_audit import codes, make_fixture

ROOT = Path(__file__).resolve().parents[1]

BLOB = "https://example.blob.core.windows.net/container/scene/asset.tif"
SAS = "st=2026-09-26T00%3A00%3A00Z&se=2026-09-27T00%3A00%3A00Z&sp=rl&sv=2024-08-04&sr=c" \
      "&skoid=11111111-aaaa&sktid=22222222-bbbb&skt=2026-09-26&ske=2026-09-27&sks=b&skv=2024-08-04" \
      "&sig=SuPeRsEcReTsIgNaTuRe%2Fabc%3D"
SIGNED = f"{BLOB}?{SAS}"
AWS_SIGNED = (
    "https://bucket.s3.us-west-2.amazonaws.com/naip/asset.tif?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAEXAMPLEKEY%2F20260926%2Fus-west-2%2Fs3%2Faws4_request"
    "&X-Amz-Date=20260926T000000Z&X-Amz-Expires=3600&X-Amz-SignedHeaders=host"
    "&X-Amz-Security-Token=FwoGZXIvYXdzSESSIONTOKEN&X-Amz-Signature=deadbeefcafef00d"
)
SECRETS = ["SuPeRsEcReTsIgNaTuRe", "AKIAEXAMPLEKEY", "deadbeefcafef00d", "FwoGZXIvYXdzSESSIONTOKEN",
           "11111111-aaaa", "22222222-bbbb", "tok3n-value", "hunter2"]


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked in: {text}"


def remote_fixture(tmp_path, href=BLOB):
    """Known-good local GeoTIFF + an Item whose asset href is a remote URL."""
    item_path, item = make_fixture(tmp_path)
    local_tif = str(tmp_path / "asset.tif")
    item["assets"]["data"]["href"] = href
    item_path.write_text(json.dumps(item))
    return item_path, item, local_tif


# --- default behaviour unchanged ---------------------------------------------------------

def test_identity_default_is_unchanged(tmp_path):
    item_path, _ = make_fixture(tmp_path)
    base = audit_item(str(item_path))
    assert audit_item(str(item_path), resolver=identity).to_dict() == base.to_dict()
    assert base.ok and base.checked_assets == 1 and codes(base) == []


def test_identity_known_bad_unchanged():
    bad = str(ROOT / "demo_bad" / "item.json")
    assert codes(audit_item(bad, resolver=identity)) == codes(audit_item(bad)) == ["BAND_COUNT_MISMATCH"]


# --- resolver is used for opening --------------------------------------------------------

def test_resolver_output_is_opened_and_context_is_passed(tmp_path):
    item_path, _, local_tif = remote_fixture(tmp_path)
    seen = []

    def resolver(href, context):
        seen.append((href, context))
        return local_tif

    result = audit_item(str(item_path), resolver=resolver)
    assert result.ok and result.checked_assets == 1 and codes(result) == []
    [(href, ctx)] = seen
    assert href == BLOB
    assert isinstance(ctx, HrefContext)
    assert (ctx.asset_key, ctx.item_id, ctx.source) == ("data", "fixture", str(item_path))
    assert ctx.asset["href"] == BLOB
    with pytest.raises(TypeError):
        ctx.asset["href"] = "mutated"  # read-only view of the STAC asset


def test_resolver_known_bad_still_semantic_error(tmp_path):
    item_path, item, local_tif = remote_fixture(tmp_path)
    item["assets"]["data"]["raster:bands"].append({"data_type": "uint16"})
    item_path.write_text(json.dumps(item))
    result = audit_item(str(item_path), resolver=lambda h, c: local_tif)
    assert [f.code for f in result.errors] == ["BAND_COUNT_MISMATCH"]


def test_resolver_not_called_for_skipped_assets(tmp_path):
    item_path, item, local_tif = remote_fixture(tmp_path)
    item["assets"]["thumbnail"] = {"href": "https://example.com/t.png", "type": "image/png", "roles": ["thumbnail"]}
    item["assets"]["overview"] = {"href": "https://example.com/o.tif", "type": "image/tiff", "roles": ["overview"]}
    calls = []
    audit_item_dict(item, source=str(item_path), resolver=lambda h, c: calls.append(c.asset_key) or local_tif)
    assert calls == ["data"]


def test_signed_href_never_in_declared_or_result(tmp_path):
    item_path, _, local_tif = remote_fixture(tmp_path)

    def sign_then_fail(href, context):
        return f"{href}?{SAS}"

    def fake_open(path, *a, **k):
        raise RasterioIOError(f"'/vsicurl/{path}' not recognized as being in a supported file format")

    import stac_integrity.audit as audit_mod
    orig = audit_mod.rasterio.open
    audit_mod.rasterio.open = fake_open
    try:
        result = audit_item(str(item_path), resolver=sign_then_fail)
    finally:
        audit_mod.rasterio.open = orig
    [finding] = result.findings
    assert finding.code == "ASSET_UNREADABLE" and finding.severity == "WARN"
    assert finding.declared == BLOB  # original, unsigned STAC href
    assert_no_secrets(json.dumps(result.to_dict()))
    assert_no_secrets(finding.message)
    assert "sig=REDACTED" in finding.message


# --- failures are never semantic errors --------------------------------------------------

def test_resolver_exception_is_warn(tmp_path):
    item_path, _, _ = remote_fixture(tmp_path)

    def broken(href, context):
        raise RuntimeError(f"token service unavailable for {href}?token=tok3n-value")

    result = audit_item(str(item_path), resolver=broken)
    assert result.ok
    [finding] = result.findings
    assert (finding.severity, finding.code, finding.asset) == ("WARN", "ASSET_RESOLVE_FAILED", "data")
    assert finding.declared == BLOB
    assert_no_secrets(json.dumps(result.to_dict()))


def test_resolver_failure_follows_fail_unreadable(tmp_path):
    item_path, _, _ = remote_fixture(tmp_path)
    result = audit_item(str(item_path), resolver=lambda h, c: 1 / 0, unreadable_severity="ERROR")
    assert [(f.severity, f.code) for f in result.findings] == [("ERROR", "ASSET_RESOLVE_FAILED")]


@pytest.mark.parametrize("bad_value", [None, "", 42])
def test_resolver_bad_return_value_is_warn(tmp_path, bad_value):
    item_path, _, _ = remote_fixture(tmp_path)
    result = audit_item(str(item_path), resolver=lambda h, c: bad_value)
    assert [(f.severity, f.code) for f in result.findings] == [("WARN", "ASSET_RESOLVE_FAILED")]


def test_auth_failure_403_is_warn_and_redacted(tmp_path, monkeypatch, capsys):
    item_path, _, _ = remote_fixture(tmp_path, href=SIGNED)  # catalog publishes a signed href itself

    def forbidden(path, *a, **k):
        raise RasterioIOError(f"HTTP response code: 403 - Server returned nothing for {path}")

    monkeypatch.setattr(rasterio, "open", forbidden)
    result = audit_item(str(item_path))
    [finding] = result.findings
    assert (finding.severity, finding.code) == ("WARN", "ASSET_UNREADABLE")
    assert "403" in finding.message
    assert finding.declared.startswith(BLOB + "?") and "sig=REDACTED" in finding.declared
    assert_no_secrets(repr(result.findings))

    for extra in ([], ["--json"]):
        assert main(["item", str(item_path), *extra]) == 0
        out = capsys.readouterr()
        assert_no_secrets(out.out + out.err)


def test_local_catalog_resolved_to_remote_failure_is_warn(tmp_path, monkeypatch):
    item_path, _ = make_fixture(tmp_path)  # relative local href in a local catalog
    monkeypatch.setattr(rasterio, "open", lambda p, *a, **k: (_ for _ in ()).throw(RasterioIOError("403")))
    result = audit_item(str(item_path), resolver=lambda h, c: AWS_SIGNED)
    assert [(f.severity, f.code) for f in result.findings] == [("WARN", "ASSET_UNREADABLE")]


def test_collection_threads_resolver(tmp_path):
    collection = str(ROOT / "demo_collection_bad" / "collection.json")
    calls = []
    result = audit_collection(collection, resolver=lambda h, c: calls.append(c.asset_key) or h)
    assert calls and result.codes == audit_collection(collection).codes


# --- redaction ---------------------------------------------------------------------------

def test_redact_azure_sas():
    out = redact(f"Failed to open {SIGNED}: 403")
    assert_no_secrets(out)
    assert out.startswith(f"Failed to open {BLOB}?st=REDACTED&se=REDACTED&sp=REDACTED")
    assert out.endswith("sig=REDACTED: 403")


def test_redact_aws_presigned_and_misc():
    assert_no_secrets(redact(AWS_SIGNED))
    assert "X-Amz-Signature=REDACTED" in redact(AWS_SIGNED)
    assert_no_secrets(redact("https://user:hunter2@host/a.tif"))
    assert redact("https://user:hunter2@host/a.tif") == f"https://{REDACTED}@host/a.tif"
    assert_no_secrets(redact("GDAL_HTTP_HEADERS=Authorization: Bearer tok3n-value"))
    assert_no_secrets(redact("header Bearer tok3n-value sent"))
    assert_no_secrets(redact("https://h/a.tif?access_token=tok3n-value&band=1"))


def test_redact_keeps_benign_urls():
    for text in [
        "https://example.com/a.tif",
        "https://example.com/a.tif?band=1&version=2",
        "s3://bucket/key.tif",
        "/local/path/asset.tif",
        "",
    ]:
        assert redact(text) == text


# --- built-in resolvers ------------------------------------------------------------------

def _stub_pc(monkeypatch):
    stub = types.ModuleType("planetary_computer")
    stub.calls = []

    def sign(href):
        stub.calls.append(href)
        return f"{href}?{SAS}"

    stub.sign = sign
    monkeypatch.setitem(sys.modules, "planetary_computer", stub)
    return stub


def test_planetary_computer_resolver_signs(monkeypatch):
    stub = _stub_pc(monkeypatch)
    resolver = planetary_computer_resolver()
    assert resolver(BLOB, HrefContext("data", "i", "s")) == SIGNED
    assert stub.calls == [BLOB]


def test_planetary_computer_resolver_missing_package(monkeypatch):
    monkeypatch.setitem(sys.modules, "planetary_computer", None)  # import raises ImportError
    with pytest.raises(ResolverUnavailableError, match=r"stac-integrity-gate\[planetary-computer\]"):
        planetary_computer_resolver()


def test_planetary_computer_end_to_end_with_stub(tmp_path, monkeypatch):
    stub = _stub_pc(monkeypatch)
    item_path, _, local_tif = remote_fixture(tmp_path)
    opened = []
    real_open = rasterio.open

    def fake_open(path, *a, **k):
        opened.append(path)
        return real_open(local_tif, *a, **k)  # the "signed blob" is served locally

    monkeypatch.setattr(rasterio, "open", fake_open)
    result = audit_item(str(item_path), resolver=planetary_computer_resolver())
    assert result.ok and codes(result) == []
    assert opened == [SIGNED] and stub.calls == [BLOB]
    assert_no_secrets(json.dumps(result.to_dict()))


def test_alternate_resolver_prefers_named_alternate(tmp_path):
    ctx_asset = {"href": "s3://eodata/x.tif", "alternate": {"https": {"href": "alt/x.tif"}}}
    resolver = alternate_resolver("https")
    source = str(tmp_path / "item.json")
    assert resolver("s3://eodata/x.tif", HrefContext("d", "i", source, ctx_asset)) == str(tmp_path / "alt" / "x.tif")
    remote_ctx = HrefContext("d", "i", "https://stac.example/items/i.json", ctx_asset)
    assert resolver("s3://eodata/x.tif", remote_ctx) == "https://stac.example/items/alt/x.tif"
    # no such alternate: fall back to the original href
    assert alternate_resolver("s3")("s3://eodata/x.tif", remote_ctx) == "s3://eodata/x.tif"
    assert resolver("h", HrefContext("d", "i", source)) == "h"


def test_alternate_resolver_end_to_end(tmp_path):
    item_path, item, _ = remote_fixture(tmp_path, href="s3://eodata/Sentinel-2/asset.tif")
    item["assets"]["data"]["alternate"] = {"https": {"href": "asset.tif"}}
    item_path.write_text(json.dumps(item))
    assert codes(audit_item(str(item_path), resolver=alternate_resolver("https"))) == []


# --- CLI ---------------------------------------------------------------------------------

LOCAL_TARGET: dict[str, str] = {}


def cli_resolver(href, context):
    """Referenced by the CLI as 'test_resolvers:cli_resolver'."""
    return LOCAL_TARGET["tif"]


def test_load_resolver_specs(monkeypatch):
    assert load_resolver("identity") is identity
    assert load_resolver("test_resolvers:cli_resolver") is cli_resolver
    assert load_resolver("alternate-https")("h", HrefContext("d", "i", "s")) == "h"
    _stub_pc(monkeypatch)
    assert callable(load_resolver("planetary-computer"))
    for bad in ["nope", "module:", ":fn"]:
        with pytest.raises(ValueError):
            load_resolver(bad)
    with pytest.raises(TypeError):
        load_resolver("test_resolvers:ROOT.name")


def test_cli_resolver_import_path(tmp_path, capsys, monkeypatch):
    item_path, _, local_tif = remote_fixture(tmp_path)
    monkeypatch.setitem(LOCAL_TARGET, "tif", local_tif)
    assert main(["item", str(item_path), "--resolver", "test_resolvers:cli_resolver"]) == 0
    assert "checked: 1" in capsys.readouterr().out


def test_cli_planetary_computer(tmp_path, capsys, monkeypatch):
    _stub_pc(monkeypatch)
    item_path, _, local_tif = remote_fixture(tmp_path)
    real_open = rasterio.open
    monkeypatch.setattr(rasterio, "open", lambda p, *a, **k: real_open(local_tif, *a, **k))
    assert main(["item", str(item_path), "--resolver", "planetary-computer", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["checked_assets"] == 1
    assert_no_secrets(out)


def test_cli_resolver_errors_exit_2(tmp_path, capsys, monkeypatch):
    item_path, _ = make_fixture(tmp_path)
    monkeypatch.setitem(sys.modules, "planetary_computer", None)
    assert main(["item", str(item_path), "--resolver", "planetary-computer"]) == 2
    assert "planetary-computer" in capsys.readouterr().err
    assert main(["item", str(item_path), "--resolver", "no_such_module_xyz:fn"]) == 2
    assert main(["collection", str(item_path), "--resolver", "bogus"]) == 2


def test_cli_operational_error_is_redacted(capsys):
    assert main(["item", f"/nonexistent/item.json?sig=SuPeRsEcReTsIgNaTuRe"]) == 2
    assert_no_secrets(capsys.readouterr().err)
