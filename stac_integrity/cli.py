from __future__ import annotations

import argparse
import sys

from . import __version__
from .audit import audit_item
from .collection import DEFAULT_SCAN_LIMIT, DEFAULT_WORKERS, SAMPLE_MODES, CollectionAuditResult, audit_collection
from .redaction import redact
from .resolvers import load_resolver
from .report import FORMATS, gate_failed, render, render_text, write_report


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--asset", action="append", dest="assets", help="Audit only this asset key (repeatable)")
    p.add_argument("--json", action="store_const", const="json", dest="format", default="text", help="Alias of --format json")
    p.add_argument("--format", choices=FORMATS, default="text", help="Report format (default: text)")
    p.add_argument(
        "--output",
        metavar="PATH",
        help="Write the --format report to PATH; stdout then gets the text summary",
    )
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
    p.add_argument(
        "--resolver",
        metavar="SPEC",
        help="Href resolver for signed/authenticated assets: 'planetary-computer', "
        "'alternate-<name>' (e.g. alternate-https) or an import path 'module:function'",
    )


def _collection_scale(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("sampling and politeness")
    g.add_argument("--sample", choices=SAMPLE_MODES, default="first", help="Item selection: first N in publisher order (default) or a uniform random sample from the scan pool")
    g.add_argument("--seed", type=int, default=None, help="Seed for --sample random (auto-generated and reported when omitted)")
    g.add_argument("--scan-limit", type=int, default=DEFAULT_SCAN_LIMIT, help=f"--sample random: candidate pool = first N Items in publisher order (default: {DEFAULT_SCAN_LIMIT})")
    g.add_argument("--page-size", type=int, default=None, help="STAC API items page size (sets ?limit= on the first items request; default: server default for first, 100 for random)")
    rate = g.add_mutually_exclusive_group()
    rate.add_argument("--max-rps", type=float, default=None, help="Max STAC JSON requests + asset opens per second (default: unlimited)")
    rate.add_argument("--delay", type=float, default=None, help="Minimum seconds between STAC JSON requests + asset opens")
    g.add_argument("--retries", type=int, default=2, help="Retries for transient STAC JSON failures: timeouts, 429, 5xx (default: 2)")


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
    collection.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Parallel Item workers; also bounds concurrent asset opens (default: {DEFAULT_WORKERS})")
    collection.add_argument("--summary-only", action="store_true", help="Omit per-item JSON results")
    _collection_scale(collection)
    _common(collection)
    return p


def _print_collection_summary(result) -> None:
    sm = result.sampling
    sampling = f"sampling: {sm.get('mode')}"
    if sm.get("mode") == "random":
        exhausted = "whole collection" if sm.get("pool_exhausted") else "first-N pool only"
        sampling += f" seed={sm.get('seed')} pool={sm.get('pool_size')} ({exhausted}, scan-limit={sm.get('scan_limit')})"
    sampling += f" | limit: {sm.get('limit')} | selected: {sm.get('selected')}"
    print(sampling)
    s = result.summary()
    print(
        f"summary: items: {s['items_checked']} | assets-opened: {s['assets_opened']} | cache-hits: {s['asset_cache_hits']} | "
        f"errors: {s['errors']} | warnings: {s['warnings']} | unreadable: {s['unreadable']} | "
        f"http: {s['http_requests']} req / {s['http_retries']} retries | complete: {'yes' if s['complete'] else 'no'}"
    )


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
        resolver = load_resolver(args.resolver) if args.resolver else None
        if args.command == "item":
            result = audit_item(
                args.source,
                asset_keys=args.assets,
                tolerance_px=args.tolerance_px,
                data_assets_only=data_only,
                unreadable_severity=unreadable,
                resolver=resolver,
                check_file_size=args.check_file_size,
            )
        else:
            result = audit_collection(
                args.source,
                limit=args.limit,
                workers=args.workers,
                asset_keys=args.assets,
                tolerance_px=args.tolerance_px,
                data_assets_only=data_only,
                unreadable_severity=unreadable,
                sample=args.sample,
                seed=args.seed,
                scan_limit=args.scan_limit,
                page_size=args.page_size,
                max_rps=args.max_rps,
                delay=args.delay,
                retries=args.retries,
                resolver=resolver,
                check_file_size=args.check_file_size,
            )
    except Exception as exc:
        print(redact(f"stac-integrity: {type(exc).__name__}: {exc}"), file=sys.stderr)
        return 2

    report = render(result, args.format, strict=args.strict, include_items=not getattr(args, "summary_only", False))
    if args.output:
        try:
            write_report(report, args.output)
        except OSError as exc:
            print(f"stac-integrity: cannot write report: {exc}", file=sys.stderr)
            return 2
        sys.stdout.write(render_text(result))
        text_shown = True
    else:
        sys.stdout.write(report)
        text_shown = args.format == "text"
    if isinstance(result, CollectionAuditResult):
        if text_shown:
            _print_collection_summary(result)
        for err in result.operational_errors:
            print(f"stac-integrity: incomplete audit: {err['stage']}: {err['reason']} ({err['url']})", file=sys.stderr)
    failed = gate_failed(result, args.strict)
    if isinstance(result, CollectionAuditResult) and not failed and not result.complete:
        # Some STAC documents could not be fetched: not a semantic failure,
        # but the audit is incomplete (operational, exit 2).
        return 2

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
