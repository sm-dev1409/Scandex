"""Scanner read API + public Scan API client.

Two services live here:

* The **scanner read API** (``C8_SCANNER_BASE``) - Cantor8's off-ledger index.
  ``/health`` is open; data endpoints need a token. It is an *indexed read
  model* and can lag the ledger, so its health reports ``scannerDelaySecs``.
  Anything sourced from it should be stored with that delay recorded.

* The **public Scan API** (``C8_SCAN_BASE``, the sv-proxy) - no auth,
  network-wide data published by the super validators.

Two things this client makes explicit in its result text:

* **401 vs 403.** 401 = "who are you" (no/invalid token). 403 = "not yours /
  machine-to-machine only" (valid token, no rights). They mean different things
  and lead to different fixes.
* **405 on a POST-only Scan endpoint is a wrong-verb signal, not a failure.**
  ``/api/scan/v0/amulet-rules`` and ``/open-and-issuing-mining-rounds`` are
  POST-only; a GET returns 405.
"""
from __future__ import annotations

from .auth import Authenticator
from .http import HttpClient, Response


class ScannerClient:
    def __init__(self, config, auth: Authenticator | None = None,
                 http: HttpClient | None = None):
        self.config = config
        self.auth = auth
        self.http = http or HttpClient(timeout=config.timeout)

    # -- scanner read API -------------------------------------------------

    def health(self) -> Response:
        """GET /health - open, no auth. Reports db status and scannerDelaySecs."""
        return self.http.get(self.config.scanner_base + "/health")

    def _auth_get(self, path: str) -> Response:
        headers = self.auth.auth_header() if self.auth else {}
        return self.http.get(self.config.scanner_base + path, headers=headers)

    def balance(self, party: str) -> Response:
        return self._auth_get(f"/tokens/balance/{party}")

    def balance_history(self, party: str) -> Response:
        return self._auth_get(f"/tokens/balance-history/{party}")

    def transfers(self, party: str) -> Response:
        return self._auth_get(f"/tokens/transfers/{party}")

    def transfers_history(self, party: str) -> Response:
        return self._auth_get(f"/tokens/transfers/history/{party}")

    def active_contracts(self) -> Response:
        return self._auth_get("/contracts/active")

    # -- public Scan API (no auth) ----------------------------------------

    def scans(self) -> Response:
        """GET /api/scan/v0/scans - every scan node on the network."""
        return self.http.get(self.config.scan_base + "/api/scan/v0/scans")

    def splice_instance_names(self) -> Response:
        """GET /api/scan/v0/splice-instance-names - network name and branding."""
        return self.http.get(self.config.scan_base + "/api/scan/v0/splice-instance-names")

    # POST-only endpoints. A GET returns 405 (wrong verb, not failure). We keep
    # helpers that use the correct verb so callers do not trip the 405.
    def amulet_rules(self) -> Response:
        return self.http.post_json(self.config.scan_base + "/api/scan/v0/amulet-rules", {})

    def mining_rounds(self) -> Response:
        return self.http.post_json(
            self.config.scan_base + "/api/scan/v0/open-and-issuing-mining-rounds", {})

    # -- interpretation helpers -------------------------------------------

    @staticmethod
    def interpret_auth_status(status: int) -> str:
        """Human-readable reading of an auth-related status code."""
        if status == 401:
            return ("HTTP 401 - no valid token was accepted: 'who are you?'. "
                    "This is an authentication problem.")
        if status == 403:
            return ("HTTP 403 - the token is valid but not entitled here: "
                    "'not yours', or the endpoint is machine-to-machine only. "
                    "This is a permission problem, not an authentication one.")
        if status == 405:
            return ("HTTP 405 - wrong HTTP verb (this endpoint is POST-only). "
                    "Not a failure, just the wrong method.")
        return f"HTTP {status}."
