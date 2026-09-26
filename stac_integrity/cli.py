from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .audit import audit_item
from .collection import audit_collection


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--asset", action="append", dest="assets", help="Audit only this asset key (repeatable)")
    p.add_argument("--json", action="store_true", dest="as_json", help="Emit machine-readable JSON")
    p.add_argument("--strict", action="store_true", help="Treat warnings as a failing exit status")
    p.add_argument("--fail-unreadable", action="store_true", help="Treat remote assets that cannot be opened as errors")
    p.add_argument("--all-raster-assets", action="store_true", help="Include visual/overview/thumbnail raster assets; default is data-role assets only")
    p.add_argument(
        "--check-file-size",
        action="store_true",
        help="Compare asset file:size with the actual object size (local stat or a 1-byte HTTP range request); WARN only",
    )
    p.add_argument(
        "--tolerance-px",
        type=float,
        default=0.01,
        help="Spatial tolerance in pixels for bbox/origin comparison (default: 0.01)",
    )


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="stac-integrity",
        description="Compare STAC raster declarations with actual raster asset headers.",
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command")

    item = sub.add_parser("item", help="Audit one STAC Item")
    item.add_argument("source", help="Local path or HTTP(S) URL to a STAC Item JSON")
    _common(item)

    collection = sub.add_parser("collection", help="Audit Items in a STAC Collection")
    collection.add_argument("source", help="Local path or HTTP(S) URL to a STAC Collection JSON")
    collection.add_argument("--limit", type=int, default=100, help="Maximum Items to audit (default: 100)")
    collection.add_argument("--workers", type=int, default=8, help="Parallel asset workers (default: 8)")
    collection.add_argument("--summary-only", action="store_true", help="Omit per-item JSON results")
    _common(collection)
    return p


def _legacy_to_item(argv: list[str]) -> list[str]:
    if argv and argv[0] not in {"item", "collection", "-h", "--help", "--version"}:
        return ["item", *argv]
    return argv


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    args = _parser().parse_args(_legacy_to_item(argv))
    if not args.command:
        _parser().print_help()
        return 2

    unreadable = "ERROR" if args.fail_unreadable else "WARN"
    data_only = not args.all_raster_assets

    try:
        if args.command == "item":
            result = audit_item(
                args.source,
                asset_keys=args.assets,
                tolerance_px=args.tolerance_px,
                data_assets_only=data_only,
                unreadable_severity=unreadable,
                check_file_size=args.check_file_size,
            )
            if args.as_json:
                print(json.dumps(result.to_dict(), indent=2, default=str))
            else:
                status = "PASS" if result.ok else "FAIL"
                print(
                    f"{status} {result.item_id} | checked: {result.checked_assets} | "
                    f"skipped: {result.skipped_assets} | errors: {len(result.errors)} | warnings: {len(result.warnings)}"
                )
                for finding in result.findings:
                    asset = f" asset={finding.asset}" if finding.asset else ""
                    print(f"[{finding.severity}] {finding.code}{asset} field={finding.field}: {finding.message}")
                    if finding.declared is not None or finding.actual is not None:
                        print(f"  declared={finding.declared!r}")
                        print(f"  actual={finding.actual!r}")
            failed = bool(result.errors or (args.strict and result.warnings))
        else:
            result = audit_collection(
                args.source,
                limit=args.limit,
                workers=args.workers,
                asset_keys=args.assets,
                tolerance_px=args.tolerance_px,
                data_assets_only=data_only,
                unreadable_severity=unreadable,
                check_file_size=args.check_file_size,
            )
            if args.as_json:
                print(json.dumps(result.to_dict(include_items=not args.summary_only), indent=2, default=str))
            else:
                status = "PASS" if result.ok else "FAIL"
                collection_state = "FAIL" if result.collection_assets_failed else "PASS"
                print(
                    f"{status} {result.collection_id} | collection-assets: {collection_state} | "
                    f"items: {result.items_checked} | failing-items: {result.failing_items} | "
                    f"assets: {result.checked_assets} | errors: {result.error_count} | warnings: {result.warning_count}"
                )
                if result.codes:
                    print("findings: " + ", ".join(f"{k}={v}" for k, v in result.codes.most_common()))
            failed = bool(result.error_count or (args.strict and result.warning_count))
    except Exception as exc:
        print(f"stac-integrity: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
