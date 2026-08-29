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

from .auth import Authenticator
from .config import load_config
from .diagnostics import NOT_TESTED, Diagnostics
from .errors import ConfigError, ScandexError
from .http import HttpClient
from .indexer import Indexer
from .ledger import LedgerClient
from .models import Outcome
from .store import DEFAULT_STALE_SECONDS, ScannerDB
from .webapi import serve

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

    # ---- A1 scanner subcommands (all read-only) ---------------------------
    parser.add_argument("--index", action="store_true",
                        help="Run the A1 scanner: seed the ACS on first run, "
                             "then stream updates forward. Read-only.")
    parser.add_argument("--balance", action="store_true",
                        help="Print indexed balances for --party from the local DB.")
    parser.add_argument("--history", action="store_true",
                        help="Print indexed transfer history for --party from the "
                             "local DB.")
    parser.add_argument("--db", default="scandex.db", metavar="PATH",
                        help="SQLite path used by --index / --balance / --history "
                             "(default: scandex.db).")
    parser.add_argument("--follow", action="store_true",
                        help="With --index, keep running run_once on a tick "
                             "instead of exiting after one pass.")
    parser.add_argument("--tick", type=float, default=5.0, metavar="SECONDS",
                        help="Seconds between ticks when --follow is set "
                             "(default: 5).")
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="Stop --follow after this many ticks. Useful for "
                             "demos and tests.")
    parser.add_argument("--limit", type=int, default=25,
                        help="Rows to show for --history (default: 25).")
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS,
                        metavar="SECONDS",
                        help="Age past which a still-pending transfer counts as "
                             f"stale (default: {DEFAULT_STALE_SECONDS}).")

    # ---- local JSON API for the frontend (read-only) ----------------------
    parser.add_argument("--serve", action="store_true",
                        help="Serve the indexed data as a local JSON API for a "
                             "frontend. Reads --db; never writes to it.")
    parser.add_argument("--host", default="127.0.0.1", metavar="HOST",
                        help="Bind address for --serve (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787, metavar="PORT",
                        help="Port for --serve (default: 8787).")
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


def _resolve_party(cfg, args, action: str) -> str | None:
    """The scanner subcommands all need a party. Prefer --party, fall back to
    C8_PARTY. Fail loudly if neither is set so the user does not silently
    index or query the wrong thing."""
    party = args.party or cfg.party
    if not party:
        print(
            f"--{action} needs a party. Pass --party PARTY-ID or set C8_PARTY.",
            file=sys.stderr,
        )
        return None
    return party


def _run_index(cfg, args) -> int:
    party = _resolve_party(cfg, args, "index")
    if not party:
        return 2
    logger = (lambda m: print(f"    . {m}")) if args.verbose else (
        lambda m: print(m))
    http = HttpClient(timeout=cfg.timeout)
    auth = Authenticator(cfg, http)
    ledger = LedgerClient(cfg, auth, http)
    with ScannerDB(args.db, stale_seconds=args.stale_seconds) as db:
        indexer = Indexer(db, ledger, [party], logger=logger)
        try:
            if args.follow:
                indexer.follow(tick_seconds=args.tick, max_ticks=args.max_ticks)
                return 0
            stats = indexer.run_once()
        except ScandexError as exc:
            print(f"INDEXER ERROR: {exc}", file=sys.stderr)
            return 1
        if args.as_json:
            print(json.dumps(stats.as_dict(), indent=2))
        else:
            print("Indexer pass complete.")
            print(f"  db                {args.db}")
            print(f"  party             {party}")
            print(f"  start offset      {stats.start_offset or '(none - first run)'}")
            print(f"  end offset        {stats.end_offset}")
            print(f"  seeded parties    {len(stats.seeded_parties)}")
            print(f"  seeded holdings   {stats.seeded_holdings}")
            print(f"  updates applied   {stats.updates_processed}")
            print(f"  holdings created  {stats.holdings_created}")
            print(f"  holdings archived {stats.holdings_archived}")
            print(f"  transfers logged  {stats.transfers_recorded}")
            print(f"  offers created    {stats.offers_created}")
            print(f"  offers resolved   {stats.offers_resolved}")
    return 0


def _run_balance(cfg, args) -> int:
    party = _resolve_party(cfg, args, "balance")
    if not party:
        return 2
    with ScannerDB(args.db, stale_seconds=args.stale_seconds) as db:
        rows = db.get_balance(party)
        offset = db.get_offset()
    if args.as_json:
        print(json.dumps({
            "party": party,
            "asOfOffset": offset,
            "byInstrument": [
                {
                    "instrument": r["instrument"],
                    "total": r["total"],
                    "spendable": r["spendable"],
                    "holdings": r["holding_count"],
                    "locked": r["locked_count"] or 0,
                }
                for r in rows
            ],
        }, indent=2))
        return 0
    print(f"Balance for {party}")
    print(f"  as-of offset  {offset or '(indexer has never run)'}")
    if not rows:
        print("  (no holdings recorded for this party yet)")
        return 0
    print(f"  {'instrument':<20} {'total':>14} {'spendable':>14} {'holdings':>10} "
          f"{'locked':>8}")
    for r in rows:
        print(f"  {(r['instrument'] or '?'):<20} {r['total']:>14} "
              f"{r['spendable']:>14} {r['holding_count']:>10} "
              f"{r['locked_count'] or 0:>8}")
    return 0


def _run_history(cfg, args) -> int:
    party = _resolve_party(cfg, args, "history")
    if not party:
        return 2
    with ScannerDB(args.db, stale_seconds=args.stale_seconds) as db:
        rows = db.get_transfers(party, limit=args.limit)
    if args.as_json:
        print(json.dumps({
            "party": party,
            "count": len(rows),
            "transfers": [
                {
                    "id": r["id"],
                    "updateId": r["update_id"],
                    "contractId": r["contract_id"],
                    "sender": r["sender"],
                    "receiver": r["receiver"],
                    "instrument": r["instrument"],
                    "amount": r["amount"],
                    "transferKind": r["transfer_kind"],
                    "status": r["status"],
                    "source": r["source"],
                    "recordedAt": r["recorded_at"],
                }
                for r in rows
            ],
        }, indent=2))
        return 0
    print(f"Transfer history for {party} (up to {args.limit} rows)")
    if not rows:
        print("  (no transfers recorded for this party yet)")
        return 0
    print(f"  {'when (UTC)':<19} {'kind':<7} {'status':<9} {'instrument':<12} "
          f"{'amount':>12} {'counterparty':<24} update_id")
    for r in rows:
        counterparty = r["receiver"] if r["sender"] == party else r["sender"]
        counterparty = (counterparty or "?")[:24]
        print(f"  {_short_time(r['recorded_at']):<19} {(r['transfer_kind'] or '?'):<7} "
              f"{(r['status'] or '?'):<9} {(r['instrument'] or '?'):<12} "
              f"{(r['amount'] or ''):>12} {counterparty:<24} {r['update_id'] or ''}")
    return 0


def _short_time(value: str | None) -> str:
    """ScannerDB stores full ISO-8601 with microseconds and a UTC offset, which
    is right for the API but 32 characters wide in a terminal table. Trim to
    seconds for display only."""
    if not value:
        return ""
    return value.replace("T", " ")[:19]


def _run_serve(cfg, args) -> int:
    """Serve the indexed database over a local read-only JSON API.

    The ledger client is optional but wired when a secret is configured, so
    /health and /metrics can report real drift against the live ledger end.
    Without it those fields degrade to null rather than failing the request.
    """
    ledger = None
    if cfg.has_secret:
        # A short timeout on purpose: /health and /metrics call ledger_end() on
        # every request and must degrade to a null offset quickly rather than
        # hanging the frontend when DevNet is slow or down.
        http = HttpClient(timeout=min(cfg.timeout, 5.0))
        ledger = LedgerClient(cfg, Authenticator(cfg, http), http)
    else:
        print("No C8_CLIENT_SECRET set: /health and /metrics will report a null "
              "ledgerOffset (the local database is still served normally).")
    with ScannerDB(args.db, stale_seconds=args.stale_seconds) as db:
        try:
            serve(db, host=args.host, port=args.port, ledger=ledger)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


def main(argv=None) -> int:
    _enable_utf8_output()
    args = build_parser().parse_args(argv)

    try:
        cfg = load_config(timeout=args.timeout, party=args.party)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2

    if args.index:
        return _run_index(cfg, args)
    if args.balance:
        return _run_balance(cfg, args)
    if args.history:
        return _run_history(cfg, args)
    if args.serve:
        return _run_serve(cfg, args)

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
