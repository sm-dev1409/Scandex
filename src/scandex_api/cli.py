"""Command-line interface for the Scandex Cantor8 diagnostic.

This holds the argument parsing and human/JSON formatting. It is imported both
by the ``check-cantor8`` console entry point and by ``scripts/check_cantor8.py``
(the no-install, bare-clone entry). Formatting lives here; the actual checks and
their meaning live in :mod:`scandex_api.diagnostics`.

Read-only. Nothing here ever writes to the ledger.
"""
from __future__ import annotations

import argparse
import json
import sys

from .config import load_config
from .diagnostics import NOT_TESTED, Diagnostics
from .errors import ConfigError
from .models import Outcome

_ICON = {
    Outcome.PASS: "PASS",
    Outcome.FAIL: "FAIL",
    Outcome.SKIPPED: "SKIP",
    Outcome.MANUAL: "MANUAL",
}


def _enable_utf8_output() -> None:
    """Live server data (party ids, network names, URLs) may contain non-ASCII.
    On a legacy Windows console (cp1252) a bare ``print`` of such a character
    raises UnicodeEncodeError. Switch our streams to UTF-8 where supported so
    output never crashes; harmless elsewhere."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:
            pass


def _print_header(cfg) -> None:
    print("Scandex x Cantor8 check")
    print(f"  ledger base   {cfg.base}")
    print(f"  auth (idp)    {cfg.idp}")
    print(f"  registry      {cfg.registry}")
    print(f"  scanner       {cfg.scanner_base}")
    print(f"  public scan   {cfg.scan_base}")
    print(f"  party         {cfg.party or '(none set)'}")
    print(f"  secret set    {'yes' if cfg.has_secret else 'NO - auth checks will fail'}")
    print(f"  timeout       {cfg.timeout:g}s")
    print()


def _print_results(results, verbose: bool) -> None:
    current_service = None
    for r in results:
        if r.service != current_service:
            current_service = r.service
            print(f"[{r.service}]")
        latency = f" ({r.latency_ms:.0f} ms)" if r.latency_ms is not None else ""
        status = f" {r.status_code}" if r.status_code is not None else ""
        print(f"  {_ICON[r.outcome]:<6} {r.method} {r.endpoint}{status}{latency}")
        print(f"         {r.summary}")
        if verbose:
            print(f"         -> {r.meaning}")
            print(f"         demo: {r.importance.value} . auth: "
                  f"{'required' if r.auth_required else 'none'}")
    print()


def _print_footer(results) -> None:
    counts = Diagnostics.counts(results)
    print(f"Passed: {counts['passed']}   Failed: {counts['failed']}   "
          f"Skipped: {counts['skipped']}   Manual action required: {counts['manual']}")
    print("NOT TESTED: " + ", ".join(NOT_TESTED))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_cantor8.py",
        description="Read-only Cantor8 connectivity and diagnostics for Scandex.",
    )
    parser.add_argument("--summary", action="store_true",
                        help="Print only the counts block and any failures.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print the meaning of each check and per-request logs.")
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="Emit the full result set as JSON.")
    parser.add_argument("--party", metavar="PARTY-ID",
                        help="Inspect this party (overrides C8_PARTY).")
    parser.add_argument("--write-report", action="store_true",
                        help="Write timestamped JSON + Markdown reports to reports/.")
    parser.add_argument("--timeout", type=float, metavar="SECONDS",
                        help="Per-request timeout in seconds.")
    parser.add_argument("--preview-transfer", nargs=3,
                        metavar=("FROM", "TO", "AMOUNT"),
                        help="Dry-run a transfer analysis. Submits NOTHING.")
    parser.add_argument("--instrument", default="Amulet",
                        help="Instrument for --preview-transfer (default: Amulet).")
    return parser


def _run_preview(diag: Diagnostics, args) -> int:
    sender, receiver, amount = args.preview_transfer
    preview = diag.preview_transfer(sender, receiver, amount,
                                    instrument=args.instrument)
    if args.as_json:
        print(json.dumps(preview.as_dict(), indent=2))
    else:
        print("Transfer preview (DRY RUN)")
        print(f"  sender        {preview.sender}")
        print(f"  receiver      {preview.receiver}")
        print(f"  instrument    {preview.instrument}")
        print(f"  amount        {preview.amount}")
        print(f"  available     {preview.available}")
        print(f"  spendable     {preview.spendable_after_locks} "
              f"(after excluding locked holdings)")
        print(f"  locked        {'yes' if preview.has_locked_holdings else 'no'}")
        print(f"  preapproved   {preview.receiver_preapproved}")
        print(f"  transferKind  {preview.transfer_kind}")
        print(f"  next step     {preview.next_step}")
        if preview.notes:
            print("  notes:")
            for note in preview.notes:
                print(f"    - {note}")
    print("\nNOTHING WAS SUBMITTED.")
    return 0


def main(argv=None) -> int:
    _enable_utf8_output()
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(timeout=args.timeout, party=args.party)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    diag = Diagnostics(cfg, verbose=args.verbose)

    if args.preview_transfer:
        return _run_preview(diag, args)

    results = diag.run(party=args.party)

    if args.as_json:
        print(json.dumps(diag.report_dict(results), indent=2))
        return diag.exit_code(results)

    _print_header(cfg)
    if args.summary:
        failures = [r for r in results if r.outcome == Outcome.FAIL]
        if failures:
            print("Failures:")
            for r in failures:
                print(f"  FAIL {r.service} {r.method} {r.endpoint}: {r.summary}")
            print()
    else:
        _print_results(results, verbose=args.verbose)

    if args.write_report:
        json_path, md_path = diag.write_reports()
        print(f"Report written:\n  {json_path}\n  {md_path}\n")

    _print_footer(results)
    return diag.exit_code(results)
