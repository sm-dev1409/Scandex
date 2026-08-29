"""Token standard registry client.

The registry is how a wallet gets what it needs to build a transfer. It is not
one host - it is an API that each token issuer implements. Base URL is
``C8_REGISTRY``; a ``Host`` header is sent when ``C8_REGISTRY_HOST`` is set (some
deployments route by Host).

Registries differ per token: Canton Coin (``Amulet``) is served by the scan app
/ sv-proxy with the DSO as admin, while Cantor8's own tokens (``c8ETH``,
``c8BTC``) live under the token-factory registry with their own admin party.

Shape trap encoded here: in the choice-context requests ``meta`` is a **flat**
string map - ``{"meta": {}}``. Sending ``{"meta": {"values": {}}}`` fails with
``DecodingFailure at .meta.values``.

The transfer-factory call is exposed in **preview mode only**. The resulting
factory is never exercised from this package.
"""
from __future__ import annotations

from .errors import HttpError
from .http import HttpClient, Response
from .models import Instrument


class RegistryClient:
    def __init__(self, config, http: HttpClient | None = None):
        self.config = config
        self.http = http or HttpClient(timeout=config.timeout)

    def _headers(self) -> dict:
        # The registry endpoints under /registry/... are generally public (no
        # bearer token). Only a Host header is added when configured.
        headers = {"Content-Type": "application/json"}
        if self.config.registry_host:
            headers["Host"] = self.config.registry_host
        return headers

    def _url(self, path: str) -> str:
        return self.config.registry + path

    def _get(self, path: str) -> Response:
        return self.http.get(self._url(path), headers=self._headers())

    def _post(self, path: str, body) -> Response:
        return self.http.post_json(self._url(path), body, headers=self._headers())

    @staticmethod
    def _check(resp: Response, what: str) -> Response:
        if not resp.ok:
            raise HttpError(f"{what}: HTTP {resp.status}. {resp.text()}",
                            resp.status, resp.text())
        return resp

    # -- metadata ---------------------------------------------------------

    def info(self) -> dict:
        """GET /registry/metadata/v1/info - admin, supported features, version."""
        return self._check(self._get("/registry/metadata/v1/info"), "registry info").json()

    def instruments(self) -> list[Instrument]:
        """GET /registry/metadata/v1/instruments - every token this registry serves."""
        resp = self._check(self._get("/registry/metadata/v1/instruments"), "instruments")
        data = resp.json()
        items = data.get("instruments", data) if isinstance(data, dict) else data
        out: list[Instrument] = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            out.append(Instrument(
                id=it.get("id") or it.get("instrumentId") or it.get("symbol"),
                name=it.get("name"),
                administrator=it.get("admin") or it.get("administrator"),
                decimals=it.get("decimals"),
                raw=it,
            ))
        return out

    def instrument(self, instrument_id: str) -> Instrument:
        """GET /registry/metadata/v1/instruments/{id}."""
        resp = self._check(
            self._get(f"/registry/metadata/v1/instruments/{instrument_id}"),
            f"instrument {instrument_id}")
        it = resp.json()
        return Instrument(
            id=it.get("id") or instrument_id,
            name=it.get("name"),
            administrator=it.get("admin") or it.get("administrator"),
            decimals=it.get("decimals"),
            raw=it,
        )

    # -- transfer preview (NEVER auto-submitted) --------------------------

    def transfer_factory_preview(self, choice_arguments: dict) -> dict:
        """POST /registry/transfer-instruction/v1/transfer-factory.

        **Preview only.** Returns ``factoryId``, ``transferKind`` and a
        ``choiceContext``. This package never exercises the returned factory -
        doing so would move money, which is a separate, human-approved action.
        """
        resp = self._check(
            self._post("/registry/transfer-instruction/v1/transfer-factory",
                       {"choiceArguments": choice_arguments}),
            "transfer-factory")
        return resp.json()

    # -- choice-context endpoints (implemented, documented, never auto-called) --
    # These return the context needed to accept / reject / withdraw an offer.
    # They are part of the write path, so diagnostics never calls them; they
    # exist here for completeness and future explicit tooling.

    def accept_context(self, instruction_cid: str) -> dict:
        # NOTE: meta is a FLAT map here. {"meta": {"values": {}}} => DecodingFailure.
        return self._check(
            self._post(
                f"/registry/transfer-instruction/v1/{instruction_cid}/choice-contexts/accept",
                {"meta": {}}),
            "accept-context").json()

    def reject_context(self, instruction_cid: str) -> dict:
        return self._check(
            self._post(
                f"/registry/transfer-instruction/v1/{instruction_cid}/choice-contexts/reject",
                {"meta": {}}),
            "reject-context").json()

    def withdraw_context(self, instruction_cid: str) -> dict:
        return self._check(
            self._post(
                f"/registry/transfer-instruction/v1/{instruction_cid}/choice-contexts/withdraw",
                {"meta": {}}),
            "withdraw-context").json()
