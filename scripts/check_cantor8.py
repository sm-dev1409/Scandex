#!/usr/bin/env python3
"""Scandex x Cantor8 connectivity and diagnostics (bare-clone entry point).

Read-only. This program never writes to the ledger: no transfers, no party
allocation, no grants, no accept/reject/withdraw, no submit-and-wait. Those are
listed as EXPECTED MANUAL ACTION and never executed.

Runs from a bare clone with no `pip install`:

    python scripts/check_cantor8.py
    python scripts/check_cantor8.py --summary
    python scripts/check_cantor8.py --verbose
    python scripts/check_cantor8.py --json
    python scripts/check_cantor8.py --party <party-id>
    python scripts/check_cantor8.py --write-report
    python scripts/check_cantor8.py --preview-transfer <from> <to> <amount> [--instrument Amulet]
    python scripts/check_cantor8.py --timeout 20

A1 scanner subcommands (read-only, back the whole thing with a local SQLite):

    python scripts/check_cantor8.py --index --party <party-id>
    python scripts/check_cantor8.py --index --follow --tick 5
    python scripts/check_cantor8.py --balance --party <party-id>
    python scripts/check_cantor8.py --history --party <party-id> --limit 25

All logic lives in the importable package (scandex_api.cli); this file only
puts src/ on sys.path when the package is not installed, then delegates.
"""
import sys
from pathlib import Path

# Make the package importable from a bare clone (no `pip install` needed).
try:
    import scandex_api  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scandex_api.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
