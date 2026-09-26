from __future__ import annotations

import json
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from .audit import USER_AGENT, AuditResult, audit_item_dict, load_json
from .redaction import redact
from .resolvers import HrefContext


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
        if self.collection_asset_result:
            out["collection_assets"] = self.collection_asset_result.to_dict()
        if include_items:
            out["items"] = [r.to_dict() for r in self.item_results]
        return out


def _load_collection_items(source: str, collection: dict[str, Any], limit: int) -> list[tuple[str, dict[str, Any]]]:
    items: list[tuple[str, dict[str, Any]]] = []

    # Static STAC Collection: explicit rel=item links.
    for link in collection.get("links", []):
        if len(items) >= limit:
            return items
        if not isinstance(link, dict) or link.get("rel") != "item" or not link.get("href"):
            continue
        item_source = _resolve(source, str(link["href"]))
        items.append((item_source, load_json(item_source)))

    if items:
        return items

    # STAC API Collection: rel=items points to FeatureCollection pages.
    items_href = _link(collection, "items")
    if not items_href:
        # STAC 1.1 Collections may legitimately expose Collection-level assets
        # without member Items.
        return []

    page_url = _resolve(source, items_href)
    while page_url and len(items) < limit:
        page = _fetch_json(page_url) if _is_url(page_url) else load_json(page_url)
        features = page.get("features") or []
        if not isinstance(features, list):
            raise ValueError("STAC API items response must contain a features array")
        for item in features:
            if len(items) >= limit:
                break
            if not isinstance(item, dict):
                continue
            self_href = _link(item, "self") or page_url
            items.append((_resolve(page_url, self_href), item))
        next_href = _link(page, "next")
        page_url = _resolve(page_url, next_href) if next_href else ""

    return items


def audit_collection(
    source: str,
    *,
    limit: int = 100,
    workers: int = 8,
    asset_keys: list[str] | None = None,
    tolerance_px: float = 0.01,
    data_assets_only: bool = True,
    unreadable_severity: str = "WARN",
    resolver: Callable[[str, HrefContext], str] | None = None,
) -> CollectionAuditResult:
    collection = load_json(source)
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
            resolver=resolver,
        )

    loaded = _load_collection_items(source, collection, max(1, limit))

    def run(pair: tuple[str, dict[str, Any]]) -> AuditResult:
        item_source, item = pair
        return audit_item_dict(
            item,
            source=item_source,
            asset_keys=asset_keys,
            tolerance_px=tolerance_px,
            data_assets_only=data_assets_only,
            unreadable_severity=unreadable_severity,
            resolver=resolver,
        )

    if workers <= 1:
        results = [run(pair) for pair in loaded]
    else:
        results: list[AuditResult] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, pair) for pair in loaded]
            for fut in as_completed(futures):
                results.append(fut.result())
        results.sort(key=lambda r: r.item_id)

    all_results = ([collection_asset_result] if collection_asset_result else []) + results
    return CollectionAuditResult(
        source=source,
        collection_id=collection_id,
        items_checked=len(results),
        checked_assets=sum(r.checked_assets for r in all_results),
        skipped_assets=sum(r.skipped_assets for r in all_results),
        item_results=results,
        collection_asset_result=collection_asset_result,
    )
