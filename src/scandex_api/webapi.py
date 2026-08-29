"""Local read-only JSON API over the indexed :class:`~scandex_api.store.ScannerDB`.

This is what the Scandex frontend actually calls. It is deliberately tiny and
**standard library only** (``http.server``) - the runtime package declares no
third-party dependencies, and adding Flask/FastAPI just to serve eight GET
routes would break that rule for no benefit.

Two processes, one SQLite file:

    python scripts/check_cantor8.py --index --follow      # the writer
    python scripts/serve_api.py                           # this, the reader

That is safe because :class:`ScannerDB` enables WAL mode: one writer and many
concurrent readers do not block each other. This server never writes.

Threading note: one ``ScannerDB`` (and therefore one ``sqlite3`` connection) is
opened for the whole process, not per request, and every read goes through
``_DB_LOCK``. ``sqlite3`` connections are not safe for genuinely concurrent use
even with ``check_same_thread=False``, and a lock around short read queries is
cheaper than a connection per request.

SECURITY / DEPLOYMENT NOTE - this is a local hackathon demo server:

* it sends ``Access-Control-Allow-Origin: *`` on every response so a frontend
  on any localhost port can call it during the demo;
* it has no authentication, no rate limiting, and no TLS;
* it defaults to binding ``127.0.0.1``.

Do not copy this CORS pattern (or this server) into anything reachable from a
network you do not control. A real deployment needs an explicit origin
allow-list, auth, and a proper WSGI/ASGI server in front.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from .errors import ConfigError, ScandexError
from .store import DEFAULT_STALE_SECONDS, ScannerDB

# Guards the single shared sqlite3 connection (see module docstring).
_DB_LOCK = threading.Lock()

_ROUTES = [
    "/health",
    "/metrics",
    "/parties",
    "/tokens/balance/{party}",
    "/tokens/holdings/{party}",
    "/tokens/owners",
    "/tokens/transfers/stale",
    "/tokens/transfers/{party}",
]


def _ledger_end(ledger):
    """Best-effort live ledger end.

    Returns ``(offset, note)``. ``note`` is non-None when we could **not** read
    it, so ``/health`` and ``/metrics`` can say *why* the field is null instead
    of silently reporting no drift. The API must stay up for the parts of the
    demo that do not need a live connection even when DevNet is down.
    """
    if ledger is None:
        return None, "no ledger client configured (set C8_CLIENT_SECRET to report drift)"
    try:
        return ledger.ledger_end(), None
    except ScandexError as exc:
        return None, f"ledger unreachable: {exc}"
    except Exception as exc:  # pragma: no cover - defensive; never kill a request
        return None, f"ledger read failed: {exc.__class__.__name__}"


def _int_param(params: dict, name: str, default):
    """Read one integer query parameter, falling back to ``default`` on
    anything unparseable. A bad query string must never 500."""
    raw = params.get(name, [None])[0]
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


class ScandexHandler(BaseHTTPRequestHandler):
    """GET-only JSON handler. ``db`` and ``ledger`` are bound onto a subclass by
    :func:`make_server`."""

    db: ScannerDB = None  # type: ignore[assignment]
    ledger = None
    server_version = "scandex-webapi"
    sys_version = ""
    # Set by make_server; default is silence so the test suite does not spray
    # request lines over its output.
    log_line = staticmethod(lambda _msg: None)

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):
        self.log_line(f"{self.address_string()} {fmt % args}")

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        # Demo-only wildcard CORS - see the module docstring before reusing.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_OPTIONS(self):  # noqa: N802 - CORS preflight
        self._send(204, {})

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        try:
            parsed = urlparse(self.path)
            segments = [unquote(s) for s in parsed.path.strip("/").split("/") if s]
            params = parse_qs(parsed.query)
            status, payload = self.route(segments, params)
        except Exception as exc:  # never let a bad request kill the server
            self._send(500, {"error": f"internal error: {exc.__class__.__name__}: {exc}"})
            return
        self._send(status, payload)

    # -- routing ----------------------------------------------------------

    def route(self, segments, params):
        """Map a path to a payload. Returns ``(http_status, json_payload)``."""
        if not segments:
            return 200, self._index()

        head = segments[0]

        if head == "health" and len(segments) == 1:
            return 200, self._health()

        if head == "metrics" and len(segments) == 1:
            return 200, self._metrics()

        if head == "parties" and len(segments) == 1:
            with _DB_LOCK:
                return 200, {"parties": self.db.get_parties()}

        if head == "tokens" and len(segments) >= 2:
            return self._tokens(segments[1:], params)

        return 404, self._no_route("/".join(segments))

    def _tokens(self, rest, params):
        kind = rest[0]

        if kind == "owners" and len(rest) == 1:
            instrument = params.get("instrument", [None])[0]
            with _DB_LOCK:
                return 200, {"owners": self.db.get_owners(instrument)}

        # NOTE: /tokens/transfers/stale must be matched before the {party}
        # form, otherwise "stale" would be read as a party id and always come
        # back empty.
        if kind == "transfers" and len(rest) == 2 and rest[1] == "stale":
            older = _int_param(params, "older_than_seconds", None)
            with _DB_LOCK:
                stale = self.db.get_stale_transfers(older)
                threshold = self.db.stale_seconds if older is None else older
            return 200, {"olderThanSeconds": threshold,
                         "count": len(stale), "transfers": stale}

        if len(rest) != 2 or not rest[1]:
            return 404, self._no_route("tokens/" + "/".join(rest))

        party = rest[1]

        if kind == "balance":
            instrument = params.get("instrument", [None])[0]
            with _DB_LOCK:
                rows = self.db.get_balance(party, instrument)
                known = self._party_known(party)
            if not rows and not known:
                return 404, self._unknown_party(party)
            # A known party with nothing yet is an empty list, not an error.
            return 200, {"party": party, "byInstrument": rows}

        if kind == "holdings":
            active_only = params.get("active_only", ["1"])[0] not in ("0", "false", "no")
            with _DB_LOCK:
                rows = self.db.get_holdings_raw(party, active_only=active_only)
                known = self._party_known(party)
            if not rows and not known:
                return 404, self._unknown_party(party)
            return 200, {"party": party, "activeOnly": active_only, "holdings": rows}

        if kind == "transfers":
            limit = _int_param(params, "limit", 50)
            with _DB_LOCK:
                rows = self.db.get_transfers(party, limit=limit)
                known = self._party_known(party)
            if not rows and not known:
                return 404, self._unknown_party(party)
            return 200, {"party": party, "count": len(rows), "transfers": rows}

        return 404, self._no_route("tokens/" + "/".join(rest))

    # -- payload builders -------------------------------------------------

    @staticmethod
    def _no_route(path: str) -> dict:
        return {"error": f"no such route: /{path}", "routes": sorted(_ROUTES)}

    @staticmethod
    def _unknown_party(party: str) -> dict:
        return {"error": f"unknown party: {party}",
                "hint": "the indexer has not seen this party yet"}

    def _party_known(self, party: str) -> bool:
        """Has the indexer ever seen this party? Distinguishes 'no data yet'
        (200 + empty list) from 'who?' (404)."""
        row = self.db.conn.execute(
            "SELECT 1 FROM parties WHERE party_id = ? "
            "UNION ALL SELECT 1 FROM holdings WHERE party_id = ? "
            "UNION ALL SELECT 1 FROM transfers WHERE sender = ? OR receiver = ? "
            "LIMIT 1",
            (party, party, party, party),
        ).fetchone()
        return row is not None

    def _health(self) -> dict:
        offset, note = _ledger_end(self.ledger)
        with _DB_LOCK:
            payload = self.db.get_health(offset)
        if note:
            payload["ledger_offset_note"] = note
        return payload

    def _metrics(self) -> dict:
        offset, note = _ledger_end(self.ledger)
        with _DB_LOCK:
            payload = self.db.get_metrics(offset)
        if note:
            payload["ledger_offset_note"] = note
        return payload

    def _index(self) -> dict:
        return {
            "service": "scandex local API",
            "readOnly": True,
            "database": self.db.path,
            "routes": sorted(_ROUTES),
        }


def make_server(db: ScannerDB, host: str = "127.0.0.1", port: int = 8787,
                ledger=None, logger=None) -> ThreadingHTTPServer:
    """Build (but do not start) the server.

    Tests use this to bind port 0 and run ``serve_forever`` on a thread; the
    CLI uses :func:`serve`. The ``db`` passed here is used for the lifetime of
    the server - one connection, not one per request.
    """
    handler = type("BoundScandexHandler", (ScandexHandler,), {
        "db": db,
        "ledger": ledger,
        "log_line": staticmethod(logger or (lambda _msg: None)),
    })
    return ThreadingHTTPServer((host, port), handler)


def serve(db: ScannerDB, host: str = "127.0.0.1", port: int = 8787,
          ledger=None, logger=print) -> None:
    """Serve the local JSON API until interrupted. Blocks."""
    httpd = make_server(db, host, port, ledger=ledger, logger=logger)
    bound_host, bound_port = httpd.server_address[:2]
    if logger:
        logger(f"Scandex local API on http://{bound_host}:{bound_port}  "
               f"(db={db.path}, read-only)")
        for route in _ROUTES:
            logger(f"  GET {route}")
        logger("CORS is wide open (demo server). Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


# --------------------------------------------------------------------------
#  Entry point (console script `serve-scandex-api`, and scripts/serve_api.py)
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serve_api.py",
        description="Serve the indexed Scandex database as a local JSON API.",
    )
    parser.add_argument("--db", default="scandex.db", metavar="PATH",
                        help="SQLite database the indexer writes (default: scandex.db).")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8787,
                        help="Port (default: 8787).")
    parser.add_argument("--stale-seconds", type=int, default=DEFAULT_STALE_SECONDS,
                        metavar="SECONDS",
                        help="Age past which a pending transfer counts as stale "
                             f"(default: {DEFAULT_STALE_SECONDS}).")
    parser.add_argument("--no-ledger", action="store_true",
                        help="Do not contact the ledger at all; /health and "
                             "/metrics report a null ledgerOffset.")
    return parser


def build_ledger_client(no_ledger: bool = False):
    """Build a :class:`LedgerClient` for the live-drift fields, or ``None``.

    Imported lazily so the read-only API has no import-time dependency on the
    auth/HTTP stack: serving a seeded database must work even if the ledger
    configuration is absent.
    """
    if no_ledger:
        return None
    from .auth import Authenticator
    from .config import load_config
    from .http import HttpClient
    from .ledger import LedgerClient

    cfg = load_config()
    if not cfg.has_secret:
        print("No C8_CLIENT_SECRET set: /health and /metrics will report a null "
              "ledgerOffset. Every other route still works.")
        return None
    # Short timeout on purpose: /health calls ledger_end() on every request and
    # must degrade quickly rather than hang the frontend when DevNet is slow.
    http = HttpClient(timeout=min(cfg.timeout, 5.0))
    return LedgerClient(cfg, Authenticator(cfg, http), http)


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        ledger = build_ledger_client(args.no_ledger)
    except ConfigError as exc:
        print(f"CONFIGURATION ERROR: {exc}", file=sys.stderr)
        return 2
    with ScannerDB(args.db, stale_seconds=args.stale_seconds) as db:
        try:
            serve(db, host=args.host, port=args.port, ledger=ledger)
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0
