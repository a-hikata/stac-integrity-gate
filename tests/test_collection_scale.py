"""Track D: sampling, rate limiting, retry, caching and summary (offline)."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import rasterio

from stac_integrity import collection as coll
from stac_integrity import netpolicy
from stac_integrity.cli import main
from stac_integrity.collection import audit_collection
from stac_integrity.netpolicy import (
    HeaderCache,
    JsonClient,
    OperationalFetchError,
    RateLimiter,
    RawResponse,
    WafChallengeError,
)
from test_collection import _item, _make_tif


def _collection(links, **extra):
    doc = {
        "type": "Collection",
        "stac_version": "1.1.0",
        "id": "scale",
        "description": "fixture",
        "license": "proprietary",
        "extent": {"spatial": {"bbox": [[0, 0, 1, 1]]}, "temporal": {"interval": [[None, None]]}},
        "links": links,
    }
    doc.update(extra)
    return doc


def _static_collection(tmp_path, n=10, shared_href=None):
    if shared_href is None:
        _make_tif(tmp_path / "data.tif")
    links = []
    for i in range(n):
        item = _item(f"i{i:02d}", shared_href or "data.tif")
        (tmp_path / f"i{i:02d}.json").write_text(json.dumps(item))
        links.append({"rel": "item", "href": f"i{i:02d}.json"})
    cp = tmp_path / "collection.json"
    cp.write_text(json.dumps(_collection(links)))
    return str(cp)


def _ids(result):
    return [r.item_id for r in result.item_results]


# --- D1 / D7 sampling ---------------------------------------------------------

def test_first_mode_is_default_and_unchanged(tmp_path):
    cp = _static_collection(tmp_path, n=10)
    result = audit_collection(cp, limit=3, workers=1)
    assert _ids(result) == ["i00", "i01", "i02"]
    assert result.sampling["mode"] == "first"
    assert result.sampling["seed"] is None
    assert result.sampling["pool_size"] == 3


def test_random_sample_is_deterministic_with_seed(tmp_path):
    cp = _static_collection(tmp_path, n=20)
    a = audit_collection(cp, limit=5, workers=2, sample="random", seed=7)
    b = audit_collection(cp, limit=5, workers=3, sample="random", seed=7)
    assert _ids(a) == _ids(b)
    assert len(_ids(a)) == 5
    assert a.sampling["seed"] == 7
    assert a.sampling["pool_size"] == 20 and a.sampling["pool_exhausted"] is True
    others = {tuple(_ids(audit_collection(cp, limit=5, workers=1, sample="random", seed=s))) for s in range(1, 8)}
    assert len(others) > 1  # different seeds give different samples


def test_random_without_seed_reports_reproducible_seed(tmp_path):
    cp = _static_collection(tmp_path, n=15)
    first = audit_collection(cp, limit=4, workers=1, sample="random")
    seed = first.sampling["seed"]
    assert isinstance(seed, int)
    again = audit_collection(cp, limit=4, workers=1, sample="random", seed=seed)
    assert _ids(first) == _ids(again)


def test_random_pool_is_bounded_by_scan_limit(tmp_path):
    cp = _static_collection(tmp_path, n=20)
    result = audit_collection(cp, limit=3, workers=1, sample="random", seed=1, scan_limit=6)
    assert result.sampling["pool_size"] == 6
    assert result.sampling["pool_exhausted"] is False
    assert all(i in {f"i{k:02d}" for k in range(6)} for i in _ids(result))


def test_invalid_sample_mode(tmp_path):
    cp = _static_collection(tmp_path, n=1)
    with pytest.raises(ValueError):
        audit_collection(cp, sample="stratified")


# --- STAC API paging with a fake opener --------------------------------------

class FakeApi:
    """Serves a STAC API collection with ``n`` items, ``per_page`` per page."""

    base = "https://stac.example/v1/collections/c"

    def __init__(self, tif_href, n=12, per_page=4, fail_page=None):
        self.n, self.per_page, self.fail_page = n, per_page, fail_page
        self.tif_href = tif_href
        self.urls = []

    def __call__(self, url, timeout):
        self.urls.append(url)
        if url == self.base:
            doc = _collection([{"rel": "items", "href": f"{self.base}/items"}], id="c")
            return RawResponse(200, json.dumps(doc).encode())
        assert url.startswith(f"{self.base}/items")
        page = int(url.split("page=")[1]) if "page=" in url else 0
        if page == self.fail_page:
            return RawResponse(202, b"", {"x-amzn-waf-action": "challenge"})
        start = page * self.per_page
        feats = [_item(f"api{i:02d}", self.tif_href) for i in range(start, min(self.n, start + self.per_page))]
        links = []
        if start + self.per_page < self.n:
            links.append({"rel": "next", "href": f"{self.base}/items?page={page + 1}"})
        return RawResponse(200, json.dumps({"type": "FeatureCollection", "features": feats, "links": links}).encode())


def test_api_paging_random_sample_and_page_size(tmp_path):
    _make_tif(tmp_path / "d.tif")
    api = FakeApi(str(tmp_path / "d.tif"), n=12, per_page=4)
    client = JsonClient(opener=api, sleep=lambda s: None)
    result = audit_collection(FakeApi.base, limit=3, sample="random", seed=3, scan_limit=8, page_size=4, client=client)
    assert result.sampling["pool_size"] == 8
    assert result.sampling["pages_fetched"] == 2
    assert result.items_checked == 3
    assert "limit=4" in api.urls[1]
    api2 = FakeApi(str(tmp_path / "d.tif"))
    again = audit_collection(FakeApi.base, limit=3, sample="random", seed=3, scan_limit=8,
                             client=JsonClient(opener=api2, sleep=lambda s: None))
    assert _ids(again) == _ids(result)
    assert "limit=8" in api2.urls[1]  # random mode requests larger pages by default


def test_waf_challenge_on_items_page_is_operational_not_semantic(tmp_path, monkeypatch):
    _make_tif(tmp_path / "d.tif")
    api = FakeApi(str(tmp_path / "d.tif"), n=12, per_page=4, fail_page=1)
    result = audit_collection(FakeApi.base, limit=10, client=JsonClient(opener=api, sleep=lambda s: None))
    assert result.items_checked == 4  # first page only
    assert result.error_count == 0
    assert not result.complete
    [err] = result.operational_errors
    assert err["status"] == 202 and err["attempts"] == 1
    assert sum(1 for u in api.urls if "page=1" in u) == 1  # not retried
    # CLI: incomplete audit without semantic errors exits 2, never 1.
    monkeypatch.setattr(netpolicy, "urllib_opener", FakeApi(str(tmp_path / "d.tif"), fail_page=1))
    assert main(["collection", FakeApi.base, "--json", "--summary-only"]) == 2


# --- D3 rate limiter ----------------------------------------------------------

class FakeClock:
    def __init__(self):
        self.t = 100.0
        self.sleeps = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s


def test_rate_limiter_spaces_requests_with_fake_clock():
    clock = FakeClock()
    rl = RateLimiter(2.0, clock=clock, sleep=clock.sleep)
    waits = [rl.acquire() for _ in range(4)]
    assert waits == [0.0, 0.5, 0.5, 0.5]
    clock.t += 10  # idle refills at most `burst` tokens
    assert rl.acquire() == 0.0
    assert rl.acquire() == 0.5


def test_rate_limiter_delay_and_disabled():
    assert RateLimiter.from_options(delay=0.25).rate == 4.0
    assert RateLimiter.from_options().acquire() == 0.0
    with pytest.raises(ValueError):
        RateLimiter.from_options(max_rps=1, delay=1)


def test_rate_limiter_is_thread_safe():
    clock_lock = threading.Lock()
    clock = FakeClock()

    def sleep(s):
        with clock_lock:
            clock.sleeps.append(s)

    rl = RateLimiter(10.0, clock=clock, sleep=sleep)
    threads = [threading.Thread(target=rl.acquire) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Clock never advanced: reservations are spaced 0.1s apart, FIFO.
    assert sorted(round(s, 6) for s in clock.sleeps) == [round(0.1 * k, 6) for k in range(1, 20)]


def test_cli_rejects_both_rate_flags(tmp_path):
    cp = _static_collection(tmp_path, n=1)
    with pytest.raises(SystemExit):
        main(["collection", cp, "--max-rps", "1", "--delay", "1"])


# --- D4 retry -----------------------------------------------------------------

def _scripted(responses):
    calls = []

    def opener(url, timeout):
        calls.append(url)
        r = responses[len(calls) - 1]
        if isinstance(r, BaseException):
            raise r
        return r

    return opener, calls


OK = RawResponse(200, b'{"type": "Collection", "id": "x"}')


def test_retry_429_honours_retry_after_then_succeeds():
    opener, calls = _scripted([RawResponse(429, b"", {"Retry-After": "3"}), OK])
    sleeps = []
    client = JsonClient(opener=opener, sleep=sleeps.append)
    assert client.fetch_json("https://x/c")["id"] == "x"
    assert len(calls) == 2 and sleeps == [3.0]
    assert client.retried == 1


def test_retry_5xx_with_backoff_then_succeeds():
    opener, calls = _scripted([RawResponse(503, b"busy"), RawResponse(502, b""), OK])
    sleeps = []
    client = JsonClient(opener=opener, sleep=sleeps.append, backoff=1.0)
    assert client.fetch_json("https://x/c")["id"] == "x"
    assert sleeps == [1.0, 2.0]


def test_retry_timeout_then_succeeds():
    opener, calls = _scripted([TimeoutError("timed out"), OK])
    client = JsonClient(opener=opener, sleep=lambda s: None)
    assert client.fetch_json("https://x/c")["id"] == "x"
    assert len(calls) == 2


def test_retries_are_bounded():
    opener, calls = _scripted([RawResponse(500, b"")] * 10)
    client = JsonClient(opener=opener, sleep=lambda s: None, retries=2)
    with pytest.raises(OperationalFetchError) as ei:
        client.fetch_json("https://x/c")
    assert len(calls) == 3 and ei.value.status == 500 and ei.value.attempts == 3


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_no_retry_on_4xx(status):
    opener, calls = _scripted([RawResponse(status, b"nope"), OK])
    client = JsonClient(opener=opener, sleep=lambda s: None)
    with pytest.raises(OperationalFetchError) as ei:
        client.fetch_json("https://x/c")
    assert len(calls) == 1 and ei.value.status == status


def test_no_retry_on_non_transient_exception():
    opener, calls = _scripted([ValueError("bad url"), OK])
    with pytest.raises(OperationalFetchError):
        JsonClient(opener=opener, sleep=lambda s: None).fetch_json("https://x/c")
    assert len(calls) == 1


def test_waf_202_empty_body_is_not_retried():
    opener, calls = _scripted([RawResponse(202, b"  "), OK])
    with pytest.raises(WafChallengeError):
        JsonClient(opener=opener, sleep=lambda s: None).fetch_json("https://x/c")
    assert len(calls) == 1


def test_retry_after_beyond_cap_gives_up():
    opener, calls = _scripted([RawResponse(429, b"", {"retry-after": "3600"}), OK])
    with pytest.raises(OperationalFetchError, match="exceeding cap"):
        JsonClient(opener=opener, sleep=lambda s: None).fetch_json("https://x/c")
    assert len(calls) == 1


def test_waf_on_collection_document_exits_2(monkeypatch, capsys):
    monkeypatch.setattr(netpolicy, "urllib_opener", lambda url, timeout: RawResponse(202, b""))
    assert main(["collection", "https://stac.example/c"]) == 2
    assert "WafChallengeError" in capsys.readouterr().err


# --- D5 cache / D2 concurrency bound ---------------------------------------------

def test_duplicate_hrefs_open_raster_once(tmp_path, monkeypatch):
    _make_tif(tmp_path / "shared.tif")
    cp = _static_collection(tmp_path, n=8, shared_href="shared.tif")
    calls = []
    real_open = rasterio.open

    def counting_open(href, *a, **kw):
        calls.append(href)
        return real_open(href, *a, **kw)

    monkeypatch.setattr("stac_integrity.audit.rasterio.open", counting_open)
    result = audit_collection(cp, workers=4)
    assert result.items_checked == 8 and result.checked_assets == 8
    assert len(calls) == 1
    assert result.stats["asset_opens"] == 1 and result.stats["asset_cache_hits"] == 7
    assert result.ok


def test_open_failures_are_cached_but_reported_per_asset(tmp_path, monkeypatch):
    cp = _static_collection(tmp_path, n=3, shared_href="missing.tif")
    calls = []
    real_open = rasterio.open
    monkeypatch.setattr("stac_integrity.audit.rasterio.open", lambda h, *a, **k: (calls.append(h), real_open(h, *a, **k))[1])
    result = audit_collection(cp, workers=3)
    assert len(calls) == 1
    assert result.codes["ASSET_UNREADABLE"] == 3
    assert result.unreadable_count == 3


def test_header_cache_bounds_concurrent_opens():
    active = 0
    peak = 0
    lock = threading.Lock()

    def reader(href):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return href

    cache = HeaderCache(reader, max_concurrent=2)
    threads = [threading.Thread(target=cache.get, args=(f"h{i}",)) for i in range(8)]
    threads += [threading.Thread(target=cache.get, args=("h0",)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak <= 2
    assert cache.opens == 8 and cache.hits == 4


# --- D6 summary -----------------------------------------------------------------

def test_json_adds_sampling_and_summary_blocks_additively(tmp_path, capsys):
    cp = _static_collection(tmp_path, n=6)
    assert main(["collection", cp, "--json", "--summary-only", "--sample", "random", "--seed", "5", "--limit", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    for key in ("source", "collection_id", "items_checked", "checked_assets", "skipped_assets", "ok",
                "collection_assets_failed", "failing_items", "error_count", "warning_count", "finding_codes"):
        assert key in out
    assert "items" not in out
    assert out["sampling"]["mode"] == "random" and out["sampling"]["seed"] == 5
    assert out["sampling"]["pool_size"] == 6 and out["sampling"]["selected"] == 2
    s = out["summary"]
    assert s["items_checked"] == 2 and s["assets_opened"] == 1 and s["asset_cache_hits"] == 1
    assert s["errors"] == 0 and s["unreadable"] == 0 and s["complete"] is True


def test_text_summary_lines(tmp_path, capsys):
    cp = _static_collection(tmp_path, n=6, shared_href="missing.tif")
    assert main(["collection", cp, "--sample", "random", "--seed", "9", "--limit", "3"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("FAIL scale | collection-assets: PASS | items: 3")
    assert out[1] == "findings: ASSET_UNREADABLE=3"
    assert out[2].startswith("sampling: random seed=9 pool=6 (whole collection")
    assert "unreadable: 3" in out[3] and "assets-opened: 1" in out[3]


def test_default_workers_is_conservative():
    assert coll.DEFAULT_WORKERS == 4


# --- real urllib path against a localhost server ------------------------------------

@pytest.fixture
def local_api(tmp_path):
    _make_tif(tmp_path / "d.tif")
    state = {"hits": {}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, doc, status=200, headers=None):
            body = json.dumps(doc).encode()
            self.send_response(status)
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            n = state["hits"][self.path] = state["hits"].get(self.path, 0) + 1
            base = f"http://127.0.0.1:{self.server.server_port}"
            if self.path == "/collections/c":
                if n == 1:
                    return self._json({"code": "slow down"}, 429, {"Retry-After": "0"})
                return self._json(_collection([{"rel": "items", "href": f"{base}/collections/c/items"}], id="c"))
            if self.path.startswith("/collections/c/items"):
                self.send_response(302)
                self.send_header("Location", f"{base}/search?collections=c")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path.startswith("/search"):
                feats = [_item(f"s{i}", str(tmp_path / "d.tif")) for i in range(3)]
                return self._json({"type": "FeatureCollection", "features": feats, "links": []})
            if self.path == "/waf":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self._json({"code": "NotFound"}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}", state
    server.shutdown()
    server.server_close()


def test_localhost_retry_redirect_and_status_handling(local_api):
    base, state = local_api
    client = JsonClient(sleep=lambda s: None)
    result = audit_collection(f"{base}/collections/c", limit=2, client=client)
    assert state["hits"]["/collections/c"] == 2  # 429 retried once
    assert result.items_checked == 2 and result.complete and result.ok
    with pytest.raises(WafChallengeError):
        client.fetch_json(f"{base}/waf")
    with pytest.raises(OperationalFetchError) as ei:
        client.fetch_json(f"{base}/missing")
    assert ei.value.status == 404
    assert state["hits"]["/missing"] == 1
