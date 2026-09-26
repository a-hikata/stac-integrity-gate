from __future__ import annotations

import json
import random
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from .audit import USER_AGENT, AuditResult, audit_item_dict, load_json, read_raster_header
from .netpolicy import HeaderCache, JsonClient, OperationalFetchError, RateLimiter
from .redaction import redact
from .resolvers import HrefContext

DEFAULT_WORKERS = 4
DEFAULT_SCAN_LIMIT = 1000
# --sample random pages through up to scan_limit Items; with a typical server
# default of 10 per page that is ~100 requests, so request larger pages unless
# the caller chose a page size. Servers may cap it; paging stays correct.
DEFAULT_RANDOM_PAGE_SIZE = 100
SAMPLE_MODES = ("first", "random")


def _is_url(value: str) -> bool:
    return urlparse(value).scheme in {"http", "https"}


def _resolve(base: str, href: str) -> str:
    if urlparse(href).scheme:
        return href
    if _is_url(base):
        return urljoin(base, href)
    return str((Path(base).resolve().parent / href).resolve())


def _fetch_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def _link(doc: dict[str, Any], rel: str) -> str | None:
    for link in doc.get("links", []):
        if isinstance(link, dict) and link.get("rel") == rel and link.get("href"):
            return str(link["href"])
    return None


@dataclass
class CollectionAuditResult:
    source: str
    collection_id: str
    items_checked: int
    checked_assets: int
    skipped_assets: int
    item_results: list[AuditResult]
    collection_asset_result: AuditResult | None = None
    sampling: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    operational_errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """False when STAC JSON fetches failed, i.e. the audit is partial."""
        return not self.operational_errors

    @property
    def unreadable_count(self) -> int:
        return self.codes.get("ASSET_UNREADABLE", 0)

    def summary(self) -> dict[str, Any]:
        return {
            "items_checked": self.items_checked,
            "failing_items": self.failing_items,
            "assets_checked": self.checked_assets,
            "assets_skipped": self.skipped_assets,
            "assets_opened": self.stats.get("asset_opens", 0),
            "asset_cache_hits": self.stats.get("asset_cache_hits", 0),
            "unreadable": self.unreadable_count,
            "errors": self.error_count,
            "warnings": self.warning_count,
            "finding_codes": dict(sorted(self.codes.items())),
            "http_requests": self.stats.get("http_requests", 0),
            "http_retries": self.stats.get("http_retries", 0),
            "rate_limit_wait_s": round(self.stats.get("rate_limit_wait_s", 0.0), 3),
            "complete": self.complete,
            "operational_errors": list(self.operational_errors),
        }

    def _all_results(self) -> list[AuditResult]:
        return ([self.collection_asset_result] if self.collection_asset_result else []) + self.item_results

    @property
    def error_count(self) -> int:
        return sum(len(r.errors) for r in self._all_results())

    @property
    def warning_count(self) -> int:
        return sum(len(r.warnings) for r in self._all_results())

    @property
    def failing_items(self) -> int:
        return sum(1 for r in self.item_results if not r.ok)

    @property
    def collection_assets_failed(self) -> bool:
        return bool(self.collection_asset_result and not self.collection_asset_result.ok)

    @property
    def ok(self) -> bool:
        return self.failing_items == 0 and not self.collection_assets_failed

    @property
    def codes(self) -> Counter:
        return Counter(f.code for r in self._all_results() for f in r.findings)

    def to_dict(self, include_items: bool = True) -> dict[str, Any]:
        out = {
            "source": redact(self.source),
            "collection_id": self.collection_id,
            "items_checked": self.items_checked,
            "checked_assets": self.checked_assets,
            "skipped_assets": self.skipped_assets,
            "ok": self.ok,
            "collection_assets_failed": self.collection_assets_failed,
            "failing_items": self.failing_items,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "finding_codes": dict(self.codes),
        }
        # Additive blocks (0.3.x): existing keys above are unchanged.
        out["sampling"] = dict(self.sampling)
        out["summary"] = self.summary()
        if self.collection_asset_result:
            out["collection_assets"] = self.collection_asset_result.to_dict()
        if include_items:
            out["items"] = [r.to_dict() for r in self.item_results]
        return out


class _Loader:
    """Loads STAC JSON: HTTP(S) via the polite client, local paths directly."""

    def __init__(self, client: JsonClient) -> None:
        self.client = client

    def __call__(self, source: str) -> dict[str, Any]:
        if _is_url(source):
            return self.client.fetch_json(source)
        return load_json(source)


def _with_page_size(url: str, page_size: int | None) -> str:
    if not page_size:
        return url
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "limit"]
    query.append(("limit", str(page_size)))
    return urlunparse(parsed._replace(query=urlencode(query)))


class _Reservoir:
    """Algorithm R: uniform sample of ``k`` from a stream of unknown length."""

    def __init__(self, k: int, rng: random.Random) -> None:
        self.k = k
        self.rng = rng
        self.seen = 0
        self.slots: list[tuple[int, Any]] = []

    def offer(self, value: Any) -> None:
        idx = self.seen
        self.seen += 1
        if len(self.slots) < self.k:
            self.slots.append((idx, value))
            return
        j = self.rng.randrange(self.seen)
        if j < self.k:
            self.slots[j] = (idx, value)

    def values(self) -> list[Any]:
        # Report in candidate (server) order for readability/stability.
        return [v for _, v in sorted(self.slots, key=lambda t: t[0])]


# A candidate is (item_source, item_dict_or_None). None = static link to be
# fetched later by a worker.
Candidate = tuple[str, "dict[str, Any] | None"]


def _collect_candidates(
    source: str,
    collection: dict[str, Any],
    *,
    limit: int,
    sample: str,
    rng: random.Random | None,
    scan_limit: int,
    page_size: int | None,
    loader: _Loader,
    operational_errors: list[dict[str, Any]],
) -> tuple[list[Candidate], dict[str, Any]]:
    """Select Items to audit.

    ``first``: the first ``limit`` Items in the publisher's order.
    ``random``: a uniform reservoir sample of ``limit`` Items from the first
    ``scan_limit`` candidates in the publisher's order (the *pool*).
    """
    pool_cap = limit if sample == "first" else max(limit, scan_limit)
    reservoir = _Reservoir(limit, rng) if sample == "random" and rng is not None else None
    selected: list[Candidate] = []
    info: dict[str, Any] = {"strategy": None, "pool_size": 0, "pool_exhausted": False, "pages_fetched": 0}

    def offer(candidate: Candidate) -> None:
        info["pool_size"] += 1
        if reservoir is not None:
            reservoir.offer(candidate)
        else:
            selected.append(candidate)

    # Static STAC Collection: explicit rel=item links. Item JSON is fetched
    # later, in the worker pool, only for selected links.
    item_links = [
        link for link in collection.get("links", [])
        if isinstance(link, dict) and link.get("rel") == "item" and link.get("href")
    ]
    if item_links:
        info["strategy"] = "static-item-links"
        for link in item_links[:pool_cap]:
            offer((_resolve(source, str(link["href"])), None))
        info["pool_exhausted"] = len(item_links) <= pool_cap
    else:
        items_href = _link(collection, "items")
        if items_href:
            # STAC API Collection: rel=items points to FeatureCollection pages.
            info["strategy"] = "api-items-paging"
            page_url = _with_page_size(_resolve(source, items_href), page_size)
            while page_url and info["pool_size"] < pool_cap:
                try:
                    page = loader(page_url)
                except OperationalFetchError as exc:
                    operational_errors.append({"stage": "items-page", **exc.to_dict()})
                    page_url = ""
                    break
                info["pages_fetched"] += 1
                features = page.get("features") or []
                if not isinstance(features, list):
                    raise ValueError("STAC API items response must contain a features array")
                for item in features:
                    if info["pool_size"] >= pool_cap:
                        break
                    if not isinstance(item, dict):
                        continue
                    self_href = _link(item, "self") or page_url
                    offer((_resolve(page_url, self_href), item))
                next_href = _link(page, "next")
                page_url = _resolve(page_url, next_href) if next_href else ""
            info["pool_exhausted"] = not page_url and not operational_errors
        # else: STAC 1.1 Collections may legitimately expose Collection-level
        # assets without member Items.
        else:
            info["pool_exhausted"] = True

    chosen = reservoir.values() if reservoir is not None else selected
    return chosen, info


def _load_collection_items(source: str, collection: dict[str, Any], limit: int) -> list[tuple[str, dict[str, Any]]]:
    """Backward-compatible helper: first ``limit`` Items, fully loaded."""
    loader = _Loader(JsonClient())
    errors: list[dict[str, Any]] = []
    chosen, _ = _collect_candidates(
        source, collection, limit=limit, sample="first", rng=None,
        scan_limit=limit, page_size=None, loader=loader, operational_errors=errors,
    )
    if errors:
        raise OperationalFetchError(errors[0]["url"], errors[0]["reason"], status=errors[0]["status"])
    return [(src, item if item is not None else loader(src)) for src, item in chosen]


def audit_collection(
    source: str,
    *,
    limit: int = 100,
    workers: int = DEFAULT_WORKERS,
    asset_keys: list[str] | None = None,
    tolerance_px: float = 0.01,
    data_assets_only: bool = True,
    unreadable_severity: str = "WARN",
    sample: str = "first",
    seed: int | None = None,
    scan_limit: int = DEFAULT_SCAN_LIMIT,
    page_size: int | None = None,
    max_rps: float | None = None,
    delay: float | None = None,
    retries: int = 2,
    max_concurrent_opens: int | None = None,
    client: JsonClient | None = None,
    header_reader: Any = None,
    resolver: Callable[[str, HrefContext], str] | None = None,
    check_file_size: bool = False,
) -> CollectionAuditResult:
    """Audit a STAC Collection's Collection-level assets and a sample of Items.

    Operational failures while fetching STAC JSON *after* the Collection
    document itself (items pages, static Item documents) do not raise; they
    are recorded in ``result.operational_errors`` and ``result.complete`` is
    False. They are never turned into semantic findings.
    """
    if sample not in SAMPLE_MODES:
        raise ValueError(f"sample must be one of {SAMPLE_MODES}")
    if workers < 1:
        raise ValueError("workers must be >= 1")
    limit = max(1, limit)
    if sample == "random" and seed is None:
        seed = random.SystemRandom().randrange(2**31)
    rng = random.Random(seed) if sample == "random" else None
    if sample == "random" and page_size is None:
        page_size = min(DEFAULT_RANDOM_PAGE_SIZE, max(limit, scan_limit))

    limiter = client.limiter if client is not None else RateLimiter.from_options(max_rps=max_rps, delay=delay)
    client = client or JsonClient(limiter=limiter, retries=retries)
    loader = _Loader(client)
    cache = HeaderCache(
        header_reader or read_raster_header,
        max_concurrent=max_concurrent_opens or workers,
        limiter=limiter,
    )

    collection = loader(source)
    if collection.get("type") != "Collection":
        raise ValueError("Source is not a STAC Collection")

    collection_id = str(collection.get("id", "<unknown>"))
    # STAC 1.1 includes Collection Assets in core. Audit those directly using
    # the Collection as the metadata parent; this catches dataset-level COG
    # declarations even when there are no Items.
    collection_asset_result: AuditResult | None = None
    collection_assets = collection.get("assets")
    if isinstance(collection_assets, dict) and collection_assets:
        pseudo_item = {
            "id": f"collection:{collection_id}",
            "properties": collection,
            "assets": collection_assets,
        }
        collection_asset_result = audit_item_dict(
            pseudo_item,
            source=source,
            asset_keys=asset_keys,
            tolerance_px=tolerance_px,
            data_assets_only=data_assets_only,
            unreadable_severity=unreadable_severity,
            header_reader=cache,
            resolver=resolver,
            check_file_size=check_file_size,
        )

    operational_errors: list[dict[str, Any]] = []
    candidates, info = _collect_candidates(
        source,
        collection,
        limit=limit,
        sample=sample,
        rng=rng,
        scan_limit=scan_limit,
        page_size=page_size,
        loader=loader,
        operational_errors=operational_errors,
    )

    def run(candidate: Candidate) -> AuditResult | None:
        item_source, item = candidate
        if item is None:
            try:
                item = loader(item_source)
            except OperationalFetchError as exc:
                operational_errors.append({"stage": "item", **exc.to_dict()})
                return None
        return audit_item_dict(
            item,
            source=item_source,
            asset_keys=asset_keys,
            tolerance_px=tolerance_px,
            data_assets_only=data_assets_only,
            unreadable_severity=unreadable_severity,
            header_reader=cache,
            resolver=resolver,
            check_file_size=check_file_size,
        )

    raw: list[AuditResult | None]
    if workers <= 1:
        raw = [run(c) for c in candidates]
    else:
        raw = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, c) for c in candidates]
            for fut in as_completed(futures):
                raw.append(fut.result())
    results = sorted((r for r in raw if r is not None), key=lambda r: r.item_id)

    sampling = {
        "mode": sample,
        "seed": seed,
        "limit": limit,
        "scan_limit": scan_limit if sample == "random" else None,
        "page_size": page_size,
        "strategy": info["strategy"],
        "pool_size": info["pool_size"],
        "pool_exhausted": info["pool_exhausted"],
        "pages_fetched": info["pages_fetched"],
        "selected": len(candidates),
    }
    stats = {
        "workers": workers,
        "max_concurrent_opens": max_concurrent_opens or workers,
        "max_rps": limiter.rate,
        "http_requests": client.requests,
        "http_retries": client.retried,
        "asset_opens": cache.opens,
        "asset_open_failures": cache.failures,
        "asset_cache_hits": cache.hits,
        "rate_limit_wait_s": limiter.waited,
    }

    all_results = ([collection_asset_result] if collection_asset_result else []) + results
    return CollectionAuditResult(
        source=source,
        collection_id=collection_id,
        items_checked=len(results),
        checked_assets=sum(r.checked_assets for r in all_results),
        skipped_assets=sum(r.skipped_assets for r in all_results),
        item_results=results,
        collection_asset_result=collection_asset_result,
        sampling=sampling,
        stats=stats,
        operational_errors=operational_errors,
    )
