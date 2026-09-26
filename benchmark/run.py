"""Regression runner for the benchmark manifest.

Usage (from the repository root):

    python -m benchmark.run                                   # offline suite
    python -m benchmark.run --baseline benchmark/baseline/offline.json
    python -m benchmark.run --write-baseline benchmark/baseline/offline.json
    python -m benchmark.run --live [--http-stats]             # opt-in, network
    python -m benchmark.run --mode all --case e84-s2-legacy-aot --case live-e84-s2-legacy-aot

Offline cases build their fixtures in a scratch directory and run the real CLI
(``stac_integrity.cli.main``) with ``--json``. Any HTTP(S) fetch that is not a
fixture-provided document fails the case, so the offline suite cannot touch the
network by accident. Live cases reuse ``scripts/public_benchmark.run_target``
(and its ``benchmark/targets.json`` entries) and are only run with ``--live`` /
``--mode live|all``.

Exit status: 0 when every case meets its expectation and there is no baseline
regression, 1 otherwise, 2 on usage/manifest errors.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import rasterio  # noqa: E402

import stac_integrity  # noqa: E402
from stac_integrity import cli  # noqa: E402

from benchmark.fixtures import build  # noqa: E402

SCHEMA_VERSION = 1
DEFAULT_MANIFEST = REPO_ROOT / "benchmark" / "manifest.json"
DEFAULT_TARGETS = REPO_ROOT / "benchmark" / "targets.json"
DEFAULT_OUT = REPO_ROOT / "benchmark" / "results" / "regression"
KINDS = {"offline-fixture", "live"}
SEVERITIES = {"ERROR", "WARN"}
SIGNALS = {"MISMATCH_FOUND", "NO_MISMATCH_FOUND", "INCONCLUSIVE"}


class ManifestError(ValueError):
    pass


class OfflineViolation(urllib.error.URLError):
    """Raised when an offline case attempts an unexpected network fetch."""


# --- manifest ----------------------------------------------------------------

def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    doc = json.loads(Path(path).read_text())
    if doc.get("schema_version") != SCHEMA_VERSION:
        raise ManifestError(f"unsupported manifest schema_version: {doc.get('schema_version')!r}")
    seen: set[str] = set()
    for case in doc.get("cases", []):
        cid = case.get("id")
        if not cid or cid in seen:
            raise ManifestError(f"missing or duplicate case id: {cid!r}")
        seen.add(cid)
        if case.get("kind") not in KINDS:
            raise ManifestError(f"{cid}: kind must be one of {sorted(KINDS)}")
        if case.get("role") not in {"known-bad", "control"}:
            raise ManifestError(f"{cid}: role must be 'known-bad' or 'control'")
        for key in ("family", "source", "expected"):
            if key not in case:
                raise ManifestError(f"{cid}: missing {key!r}")
        expected = case["expected"]
        for f in expected.get("findings", []):
            if f.get("severity") not in SEVERITIES or not f.get("code"):
                raise ManifestError(f"{cid}: bad expected finding {f!r}")
        if case["kind"] == "offline-fixture":
            if "builder" not in case.get("fixture", {}):
                raise ManifestError(f"{cid}: offline-fixture needs fixture.builder")
            if "exit_code" not in expected:
                raise ManifestError(f"{cid}: offline-fixture needs expected.exit_code")
        else:
            if "live" not in case:
                raise ManifestError(f"{cid}: live case needs a 'live' block")
            if expected.get("signal") not in SIGNALS:
                raise ManifestError(f"{cid}: live case needs expected.signal in {sorted(SIGNALS)}")
        has_error = any(f["severity"] == "ERROR" for f in expected.get("findings", []))
        if case["role"] == "control" and has_error:
            raise ManifestError(f"{cid}: a control must not expect an ERROR")
    return doc


def select_cases(manifest: dict[str, Any], mode: str, only: list[str] | None) -> list[dict[str, Any]]:
    kinds = {"offline": {"offline-fixture"}, "live": {"live"}, "all": KINDS}[mode]
    cases = [c for c in manifest["cases"] if c["kind"] in kinds]
    if only:
        unknown = set(only) - {c["id"] for c in manifest["cases"]}
        if unknown:
            raise ManifestError(f"unknown case id(s): {', '.join(sorted(unknown))}")
        cases = [c for c in cases if c["id"] in only]
    return cases


# --- findings / expectations -------------------------------------------------

def _findings_from_json(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten item or collection JSON into path-free finding records."""
    raw: list[dict[str, Any]] = list(doc.get("findings", []))
    if "collection_assets" in doc:
        raw += doc["collection_assets"].get("findings", [])
    for item in doc.get("items", []) or []:
        raw += item.get("findings", [])
    out = [{"code": f["code"], "severity": f["severity"], "asset": f.get("asset", ""),
            "field": f.get("field", "")} for f in raw]
    return sorted(out, key=lambda f: (f["code"], f["severity"], f["asset"], f["field"]))


def _pairs(findings: list[dict[str, Any]]) -> set[tuple[str, str]]:
    return {(f["code"], f["severity"]) for f in findings}


def evaluate(case: dict[str, Any], findings: list[dict[str, Any]], exit_code: int | None,
             signal: str | None = None) -> dict[str, Any]:
    """Compare observations with the manifest expectation.

    Offline cases match the (code, severity) set exactly; live cases match it as
    a subset (real catalogs can carry extra, unrelated WARNs) unless the
    manifest says ``"match": "exact"``.
    """
    expected = case["expected"]
    match = expected.get("match", "exact" if case["kind"] == "offline-fixture" else "subset")
    want = {(f["code"], f["severity"]) for f in expected.get("findings", [])}
    got = _pairs(findings)
    missing = sorted(want - got)
    unexpected = sorted(got - want) if match == "exact" else []
    # A control must never emit an ERROR, whatever the match mode.
    if case["role"] == "control":
        unexpected = sorted(set(unexpected) | {p for p in got if p[1] == "ERROR" and p not in want})
    checks: dict[str, Any] = {"match": match, "missing": [list(p) for p in missing],
                              "unexpected": [list(p) for p in unexpected]}
    passed = not missing and not unexpected
    if "exit_code" in expected:
        checks["exit_code_ok"] = exit_code == expected["exit_code"]
        passed = passed and checks["exit_code_ok"]
    if "signal" in expected:
        checks["signal_ok"] = signal == expected["signal"]
        passed = passed and checks["signal_ok"]
    checks["passed"] = passed
    return checks


# --- offline execution -------------------------------------------------------

@contextlib.contextmanager
def offline_network(documents: dict[str, str]):
    """Serve fixture documents for their URLs; refuse every other fetch."""

    def fake_urlopen(req, *args, **kwargs):
        url = req.full_url if isinstance(req, urllib.request.Request) else str(req)
        if url in documents:
            return io.BytesIO(Path(documents[url]).read_bytes())
        raise OfflineViolation(f"offline benchmark attempted a network fetch: {url}")

    with mock.patch.object(urllib.request, "urlopen", fake_urlopen):
        yield


def _sanitize(text: str, *roots: Path | str) -> str:
    for root in roots:
        text = text.replace(str(root), "<" + ("fixture" if root != REPO_ROOT else "repo") + ">")
    return text


def run_offline_case(case: dict[str, Any], scratch: Path) -> dict[str, Any]:
    workdir = scratch / case["id"]
    row: dict[str, Any] = {"id": case["id"], "kind": case["kind"], "family": case["family"],
                           "role": case["role"], "builder": case["fixture"]["builder"]}
    t0 = time.perf_counter()
    try:
        spec = build(case["fixture"]["builder"], workdir)
    except Exception as exc:
        row.update(status="ERROR", error=_sanitize(f"fixture build failed: {type(exc).__name__}: {exc}",
                                                   workdir, REPO_ROOT))
        return row
    row["build_s"] = round(time.perf_counter() - t0, 4)

    argv = [spec.command, spec.source, "--json", *spec.cli_args]
    out, err = io.StringIO(), io.StringIO()
    t1 = time.perf_counter()
    with offline_network(spec.documents), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            exit_code = cli.main(argv)
        except SystemExit as exc:  # argparse errors
            exit_code = int(exc.code or 0)
    row["audit_s"] = round(time.perf_counter() - t1, 4)
    row["exit_code"] = exit_code

    try:
        findings = _findings_from_json(json.loads(out.getvalue()))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        row.update(status="ERROR", error=_sanitize(
            f"CLI did not emit JSON ({type(exc).__name__}); stderr: {err.getvalue().strip()[:500]}", workdir, REPO_ROOT))
        return row
    row["findings"] = findings
    row["checks"] = evaluate(case, findings, exit_code)
    row["status"] = "PASS" if row["checks"]["passed"] else "FAIL"
    return row


# --- live execution ----------------------------------------------------------

def _load_public_benchmark():
    spec = importlib.util.spec_from_file_location("public_benchmark", REPO_ROOT / "scripts" / "public_benchmark.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_DOWNLOAD = re.compile(r"\b(\w+): Downloading (\d+)-(\d+) ")
_FILESIZE = re.compile(r"\b(\w+): GetFileSize\(.*response_code=(\d+)")


class HttpStats(logging.Handler):
    """Approximate GDAL HTTP accounting from CPL_DEBUG messages.

    Counts GetFileSize probes (HEAD, or a ranged GET answered with 206 whose
    body GDAL keeps -- typical for /vsis3/), explicit ranged GETs with their
    requested byte span, and STAC JSON fetches through urllib. Byte counts are
    a lower bound: the body of a 206 size probe is not logged with its span. GDAL's block
    cache means repeated reads of the same file in one process are not
    re-counted. Message formats are GDAL-version dependent; treat as indicative.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.reset()

    def reset(self) -> None:
        self.size_requests = 0
        self.size_requests_ranged = 0
        self.range_requests = 0
        self.range_bytes = 0
        self.json_requests = 0

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        m = _DOWNLOAD.search(msg)
        if m:
            self.range_requests += 1
            self.range_bytes += int(m.group(3)) - int(m.group(2)) + 1
        else:
            m = _FILESIZE.search(msg)
            if m:
                self.size_requests += 1
                if m.group(2) == "206":
                    self.size_requests_ranged += 1

    def snapshot(self) -> dict[str, int]:
        return {"gdal_size_requests": self.size_requests, "gdal_size_requests_ranged": self.size_requests_ranged,
                "gdal_range_requests": self.range_requests,
                "gdal_range_bytes": self.range_bytes, "json_requests": self.json_requests}


@contextlib.contextmanager
def http_stats_capture(enabled: bool):
    if not enabled:
        yield None
        return
    stats = HttpStats()
    logger = logging.getLogger("rasterio")
    old_level, old_env = logger.level, os.environ.get("CPL_DEBUG")
    logger.addHandler(stats)
    logger.setLevel(logging.DEBUG)
    os.environ["CPL_DEBUG"] = "ON"
    real_urlopen = urllib.request.urlopen

    def counting_urlopen(*args, **kwargs):
        stats.json_requests += 1
        return real_urlopen(*args, **kwargs)

    try:
        with mock.patch.object(urllib.request, "urlopen", counting_urlopen):
            yield stats
    finally:
        logger.removeHandler(stats)
        logger.setLevel(old_level)
        if old_env is None:
            os.environ.pop("CPL_DEBUG", None)
        else:
            os.environ["CPL_DEBUG"] = old_env


def _live_target(case: dict[str, Any], targets_path: Path) -> dict[str, Any]:
    live = dict(case["live"])
    if "target" in live:
        targets = {t["name"]: t for t in json.loads(targets_path.read_text()).get("targets", [])}
        if live["target"] not in targets:
            raise ManifestError(f"{case['id']}: target {live['target']!r} not in {targets_path.name}")
        target = dict(targets[live.pop("target")])
        target.update(live)
    else:
        target = live
    target["name"] = case["id"]
    target.pop("expected_signal", None)  # the manifest expectation is authoritative
    return target


def run_live_case(case: dict[str, Any], pb, targets_path: Path, stats: HttpStats | None) -> dict[str, Any]:
    row: dict[str, Any] = {"id": case["id"], "kind": case["kind"], "family": case["family"], "role": case["role"]}
    try:
        target = _live_target(case, targets_path)
    except ManifestError as exc:
        row.update(status="ERROR", error=str(exc))
        return row
    row["target"] = {k: target[k] for k in ("kind", "source", "assets", "limit") if k in target}
    if stats:
        stats.reset()
    t0 = time.perf_counter()
    result = pb.run_target(target)
    row["audit_s"] = round(time.perf_counter() - t0, 4)
    row["signal"] = result["signal"]
    if stats:
        row["http"] = stats.snapshot()
    if "operational_error" in result:
        row["operational_error"] = result["operational_error"]
    findings = _findings_from_json(result.get("details") or {})
    row["findings"] = findings
    row["checks"] = evaluate(case, findings, None, result["signal"])
    row["status"] = "PASS" if row["checks"]["passed"] else "FAIL"
    return row


# --- orchestration -------------------------------------------------------------

def environment() -> dict[str, Any]:
    env = {
        "stac_integrity": stac_integrity.__version__,
        "python": platform.python_version(),
        "platform": f"{platform.system()}-{platform.machine()}",
        "rasterio": rasterio.__version__,
        "gdal": rasterio.__gdal_version__,
    }
    try:
        env["git_commit"] = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5,
        ).stdout.strip() or None
    except Exception:
        env["git_commit"] = None
    return env


def run_suite(cases: list[dict[str, Any]], *, mode: str, targets_path: Path = DEFAULT_TARGETS,
              http_stats: bool = False) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    live_cases = [c for c in cases if c["kind"] == "live"]
    with tempfile.TemporaryDirectory(prefix="stac-integrity-bench-") as tmp:
        for case in cases:
            if case["kind"] == "offline-fixture":
                rows.append(run_offline_case(case, Path(tmp)))
    if live_cases:
        pb = _load_public_benchmark()
        pb.configure_anonymous_access()
        with http_stats_capture(http_stats) as stats:
            for case in live_cases:
                rows.append(run_live_case(case, pb, targets_path, stats))
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("PASS", "FAIL", "ERROR")}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "mode": mode,
        "environment": environment(),
        "summary": {"total": len(rows), **{k.lower(): v for k, v in counts.items()},
                    "audit_s_total": round(sum(r.get("audit_s", 0) for r in rows), 4)},
        "cases": rows,
    }


# --- baseline comparison -------------------------------------------------------

def _unexpected_errors(row: dict[str, Any]) -> set[tuple[str, str]]:
    return {tuple(p) for p in row.get("checks", {}).get("unexpected", []) if p[1] == "ERROR"}


def _missing_errors(row: dict[str, Any]) -> set[tuple[str, str]]:
    return {tuple(p) for p in row.get("checks", {}).get("missing", []) if p[1] == "ERROR"}


def _severities_by_code(row: dict[str, Any]) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for f in row.get("findings", []):
        out.setdefault(f["code"], set()).add(f["severity"])
    return out


def compare(baseline: dict[str, Any], current: dict[str, Any], *, timing_factor: float = 2.0,
            timing_min_s: float = 0.25, fail_on_timing: bool = False, partial: bool = False) -> dict[str, Any]:
    """Diff two results documents case by case.

    Regressions: new false positives (an ERROR the manifest does not expect,
    e.g. on a control), new false negatives (an expected ERROR that is now
    missing), changed severities for a code present in both runs, changed exit
    codes / live signals, cases that went PASS -> FAIL/ERROR, and cases that
    disappeared (unless ``partial``). Timing regressions (audit time grew by
    more than ``timing_factor`` and by at least ``timing_min_s``) are reported,
    and only counted as regressions with ``fail_on_timing``.
    Improvements: resolved false positives / negatives, FAIL -> PASS.
    """
    base = {r["id"]: r for r in baseline.get("cases", [])}
    cur = {r["id"]: r for r in current.get("cases", [])}
    diff: dict[str, list[Any]] = {k: [] for k in (
        "new_false_positives", "resolved_false_positives", "new_false_negatives", "resolved_false_negatives",
        "changed_severities", "changed_exit_codes", "changed_signals", "newly_failing", "newly_passing",
        "timing_regressions", "cases_added", "cases_removed")}
    diff["cases_added"] = sorted(set(cur) - set(base))
    diff["cases_removed"] = [] if partial else sorted(set(base) - set(cur))
    for cid in sorted(set(base) & set(cur)):
        b, c = base[cid], cur[cid]
        for code, sev in sorted(_unexpected_errors(c) - _unexpected_errors(b)):
            diff["new_false_positives"].append({"case": cid, "role": c["role"], "code": code, "severity": sev})
        for code, sev in sorted(_unexpected_errors(b) - _unexpected_errors(c)):
            diff["resolved_false_positives"].append({"case": cid, "code": code, "severity": sev})
        for code, sev in sorted(_missing_errors(c) - _missing_errors(b)):
            diff["new_false_negatives"].append({"case": cid, "code": code, "severity": sev})
        for code, sev in sorted(_missing_errors(b) - _missing_errors(c)):
            diff["resolved_false_negatives"].append({"case": cid, "code": code, "severity": sev})
        bs, cs = _severities_by_code(b), _severities_by_code(c)
        for code in sorted(set(bs) & set(cs)):
            if bs[code] != cs[code]:
                diff["changed_severities"].append({"case": cid, "code": code,
                                                   "baseline": sorted(bs[code]), "current": sorted(cs[code])})
        if b.get("exit_code") != c.get("exit_code") and "exit_code" in b:
            diff["changed_exit_codes"].append({"case": cid, "baseline": b.get("exit_code"), "current": c.get("exit_code")})
        if b.get("signal") != c.get("signal") and "signal" in b:
            diff["changed_signals"].append({"case": cid, "baseline": b.get("signal"), "current": c.get("signal")})
        if b.get("status") == "PASS" and c.get("status") != "PASS":
            diff["newly_failing"].append({"case": cid, "current": c.get("status")})
        if b.get("status") != "PASS" and c.get("status") == "PASS":
            diff["newly_passing"].append({"case": cid, "baseline": b.get("status")})
        bt, ct = b.get("audit_s"), c.get("audit_s")
        if bt is not None and ct is not None and ct > bt * timing_factor and ct - bt >= timing_min_s:
            diff["timing_regressions"].append({"case": cid, "baseline_s": bt, "current_s": ct,
                                               "factor": round(ct / bt, 2) if bt else None})
    regression_keys = ["new_false_positives", "new_false_negatives", "changed_severities", "changed_exit_codes",
                       "changed_signals", "newly_failing", "cases_removed"]
    if fail_on_timing:
        regression_keys.append("timing_regressions")
    return {
        "baseline_generated_at": baseline.get("generated_at"),
        "baseline_environment": baseline.get("environment"),
        "timing": {"factor": timing_factor, "min_s": timing_min_s, "fail_on_timing": fail_on_timing},
        "regressions": sum(len(diff[k]) for k in regression_keys),
        "improvements": sum(len(diff[k]) for k in ("resolved_false_positives", "resolved_false_negatives", "newly_passing")),
        **diff,
    }


# --- reporting -----------------------------------------------------------------

def _fmt_pairs(pairs) -> str:
    return ", ".join(f"{c}:{s}" for c, s in pairs) or "—"


def markdown(results: dict[str, Any], diff: dict[str, Any] | None = None) -> str:
    env = results["environment"]
    s = results["summary"]
    lines = [
        "# stac-integrity-gate regression benchmark", "",
        f"Generated {results['generated_at']} · mode `{results['mode']}` · stac-integrity {env['stac_integrity']} · "
        f"rasterio {env['rasterio']} / GDAL {env['gdal']} · Python {env['python']}", "",
        f"**{s['pass']} pass / {s['fail']} fail / {s['error']} error** of {s['total']} cases; "
        f"audit time {s['audit_s_total']:.2f} s", "",
        "| Case | Role | Status | Findings (code:severity) | Exit / signal | Audit s |",
        "|---|---|---|---|---|---:|",
    ]
    for r in results["cases"]:
        pairs = sorted({(f["code"], f["severity"]) for f in r.get("findings", [])})
        outcome = r.get("signal") or r.get("exit_code", "—")
        status = r["status"]
        checks = r.get("checks", {})
        if checks.get("missing") or checks.get("unexpected"):
            status += f" (missing {_fmt_pairs(checks.get('missing', []))}; unexpected {_fmt_pairs(checks.get('unexpected', []))})"
        if r.get("error"):
            status += f" ({r['error']})"
        lines.append(f"| {r['id']} | {r['role']} | {status} | {_fmt_pairs(pairs)} | {outcome} | {r.get('audit_s', 0):.3f} |")
    http_rows = [r for r in results["cases"] if "http" in r]
    if http_rows:
        lines += ["", "## HTTP (approximate, from GDAL CPL_DEBUG)", "",
                  "| Case | JSON fetches | GDAL size probes (206) | GDAL range GETs | Range bytes (lower bound) |",
                  "|---|---:|---:|---:|---:|"]
        for r in http_rows:
            h = r["http"]
            lines.append(f"| {r['id']} | {h['json_requests']} | {h['gdal_size_requests']} ({h['gdal_size_requests_ranged']}) | "
                         f"{h['gdal_range_requests']} | {h['gdal_range_bytes']} |")
    if diff is not None:
        lines += ["", "## Baseline comparison", "",
                  f"Baseline generated {diff['baseline_generated_at']}. "
                  f"**Regressions: {diff['regressions']}**, improvements: {diff['improvements']}.", ""]
        labels = [("new_false_positives", "New false positives"), ("new_false_negatives", "New false negatives"),
                  ("changed_severities", "Changed severities"), ("changed_exit_codes", "Changed exit codes"),
                  ("changed_signals", "Changed live signals"), ("newly_failing", "Newly failing"),
                  ("cases_removed", "Cases removed"), ("timing_regressions", "Timing regressions"),
                  ("resolved_false_positives", "Resolved false positives"),
                  ("resolved_false_negatives", "Resolved false negatives"), ("newly_passing", "Newly passing"),
                  ("cases_added", "Cases added")]
        for key, label in labels:
            items = diff[key]
            lines.append(f"- {label}: {len(items)}")
            for it in items:
                lines.append(f"  - `{json.dumps(it, sort_keys=True)}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m benchmark.run", description=__doc__.split("\n\n")[0])
    p.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    p.add_argument("--targets", type=Path, default=DEFAULT_TARGETS, help="live target definitions (targets.json)")
    p.add_argument("--mode", choices=["offline", "live", "all"], default="offline")
    p.add_argument("--live", action="store_const", const="live", dest="mode", help="shorthand for --mode live")
    p.add_argument("--case", action="append", dest="cases", help="run only this case id (repeatable)")
    p.add_argument("--out-dir", type=Path, default=DEFAULT_OUT, help="where results.json / results.md go")
    p.add_argument("--baseline", type=Path, help="results.json to compare against")
    p.add_argument("--write-baseline", type=Path, help="also write this run's results to PATH (new baseline)")
    p.add_argument("--timing-factor", type=float, default=2.0, help="flag cases slower than baseline x FACTOR")
    p.add_argument("--timing-min-seconds", type=float, default=0.25, help="ignore slowdowns smaller than this")
    p.add_argument("--fail-on-timing", action="store_true", help="count timing regressions as failures")
    p.add_argument("--http-stats", action="store_true", help="live: approximate HTTP request/byte counts via GDAL CPL_DEBUG")
    args = p.parse_args(argv)

    try:
        manifest = load_manifest(args.manifest)
        cases = select_cases(manifest, args.mode, args.cases)
    except (ManifestError, OSError, json.JSONDecodeError) as exc:
        print(f"benchmark: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("benchmark: no cases selected", file=sys.stderr)
        return 2

    results = run_suite(cases, mode=args.mode, targets_path=args.targets, http_stats=args.http_stats)
    diff = None
    if args.baseline:
        baseline = json.loads(args.baseline.read_text())
        diff = compare(baseline, results, timing_factor=args.timing_factor, timing_min_s=args.timing_min_seconds,
                       fail_on_timing=args.fail_on_timing,
                       partial=bool(args.cases) or baseline.get("mode") != args.mode)
        results["baseline_diff"] = diff

    args.out_dir.mkdir(parents=True, exist_ok=True)
    (args.out_dir / "results.json").write_text(json.dumps(results, indent=2, sort_keys=True) + "\n")
    report = markdown(results, diff)
    (args.out_dir / "results.md").write_text(report)
    if args.write_baseline:
        args.write_baseline.parent.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in results.items() if k != "baseline_diff"}
        args.write_baseline.write_text(json.dumps(clean, indent=2, sort_keys=True) + "\n")
    print(report)
    print(f"results: {args.out_dir / 'results.json'}")

    failed = results["summary"]["fail"] + results["summary"]["error"]
    return 1 if failed or (diff and diff["regressions"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
