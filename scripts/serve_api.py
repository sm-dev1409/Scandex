#!/usr/bin/env python3
"""Scandex local JSON API for the frontend (bare-clone entry point).

Serves the SQLite database the A1 indexer fills, over plain HTTP on localhost,
so a separate frontend process can read live scanner data. Read-only: this
process never writes to the database and never writes to the ledger.

Runs from a bare clone with no `pip install`:

    python scripts/serve_api.py
    python scripts/serve_api.py --db scandex.db --port 8787
    python scripts/serve_api.py --no-ledger

Run it alongside the indexer, which is the writer:

    python scripts/check_cantor8.py --index --follow --party <party-id>
    python scripts/serve_api.py

WAL mode (enabled by ScannerDB) is what makes that pair safe on one file.

C8_CLIENT_SECRET is optional here. With it, /health and /metrics report real
drift against the live ledger end; without it those fields come back null with
a note, and every other route still works off the local database.

WARNING: this is a hackathon demo server - no auth, no TLS, and wildcard CORS.
Keep it on 127.0.0.1. See src/scandex_api/webapi.py for the full note.

All logic lives in the importable package (scandex_api.webapi); this file only
puts src/ on sys.path when the package is not installed, then delegates.
"""
import sys
from pathlib import Path

# Make the package importable from a bare clone (no `pip install` needed).
try:
    import scandex_api  # noqa: F401
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from scandex_api.webapi import main

if __name__ == "__main__":
    raise SystemExit(main())
