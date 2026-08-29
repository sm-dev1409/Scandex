"""Canton JSON Ledger API v2 client.

The Ledger API is the *authoritative* source for current ledger state. This
client is read-only for everything the diagnostic touches. The write path
(``submit_and_wait``) and party/rights mutations are implemented so the shape is
documented and testable, but the diagnostics layer never calls them.

Correctness details encoded here were each learned the hard way (see
``TROUBLESHOOTING.md``):

* ``Holding`` is a Daml **interface**, not a template. We query it with an
  ``InterfaceFilter`` and ``includeInterfaceView: true``. A ``TemplateFilter``
  returns ``200 OK`` with an empty list - indistinguishable from a zero balance.
* We read the **ledger end first** and query active contracts *at that offset*,
  so every snapshot is internally consistent and carries the offset it was read
  at.
* Only parties with ``isLocal: true`` can submit. Submitting as a remote party
  yields ``NO_SYNCHRONIZER_ON_WHICH_ALL_SUBMITTERS_CAN_SUBMIT``.
"""
from __future__ import annotations

import uuid

from .auth import Authenticator
from .errors import HttpError, NotAuthenticatedError, PermissionError_
from .http import HttpClient, Response
from .models import Holding, HoldingsSummary, Party

# The Holding interface id. Using a TemplateFilter here silently returns [].
HOLDING_INTERFACE = "#splice-api-token-holding-v1:Splice.Api.Token.HoldingV1:Holding"


class LedgerClient:
    def __init__(self, config, auth: Authenticator, http: HttpClient | None = None):
        self.config = config
        self.auth = auth
        self.http = http or HttpClient(timeout=config.timeout)

    # -- transport helpers ------------------------------------------------

    def _get(self, path: str) -> Response:
        return self.http.get(self.config.base + path, headers=self.auth.auth_header())

    def _post(self, path: str, body) -> Response:
        return self.http.post_json(
            self.config.base + path, body, headers=self.auth.auth_header()
        )

    @staticmethod
    def _raise_for_status(resp: Response, what: str) -> Response:
        if resp.status == 401:
            raise NotAuthenticatedError(
                f"{what}: HTTP 401 - the ledger does not recognise this token "
                "(who are you?).", 401, resp.text())
        if resp.status == 403:
            raise PermissionError_(
                f"{what}: HTTP 403 - the token is valid but has no rights here "
                "(not yours, or machine-to-machine only).", 403, resp.text())
        if not resp.ok:
            raise HttpError(f"{what}: HTTP {resp.status}. {resp.text()}",
                            resp.status, resp.text())
        return resp

    # -- reads ------------------------------------------------------------

    def ledger_end(self) -> str:
        """Current ledger offset. Also the cheapest health check."""
        resp = self._raise_for_status(self._get("/v2/state/ledger-end"), "ledger-end")
        return resp.json().get("offset")

    def parties(self) -> list[Party]:
        resp = self._raise_for_status(self._get("/v2/parties"), "parties")
        details = resp.json().get("partyDetails", [])
        out = []
        for p in details:
            out.append(Party(
                party=p.get("party"),
                is_local=bool(p.get("isLocal")),
                display_name=p.get("localMetadata", {}).get("annotations", {}).get(
                    "displayName") or p.get("displayName"),
            ))
        return out

    def local_parties(self) -> list[Party]:
        """Only these can submit."""
        return [p for p in self.parties() if p.is_local]

    def find_party(self, prefix: str) -> Party | None:
        for p in self.parties():
            if p.party == prefix or p.hint == prefix or p.party.startswith(prefix):
                return p
        return None

    def active_contracts(self, party: str, offset: str, filters: list | None = None) -> list:
        """Raw active contracts for ``party`` at ``offset``.

        ``filters`` is the ``cumulative`` list; when omitted, all contracts the
        party can see are returned.
        """
        cumulative = filters if filters is not None else []
        body = {
            "filter": {"filtersByParty": {party: {"cumulative": cumulative}}},
            "verbose": False,
            "activeAtOffset": offset,
        }
        resp = self._raise_for_status(
            self._post("/v2/state/active-contracts", body), "active-contracts")
        data = resp.json()
        # The API returns a JSON array of entries.
        return data if isinstance(data, list) else data.get("result", [])

    def holdings(self, party: str, offset: str | None = None) -> HoldingsSummary:
        """A consistent snapshot of a party's Holding contracts.

        Reads the ledger end first (unless ``offset`` is supplied) so the
        snapshot is internally consistent, and records that offset.
        """
        if offset is None:
            offset = self.ledger_end()
        interface_filter = [{
            "identifierFilter": {"InterfaceFilter": {"value": {
                "interfaceId": HOLDING_INTERFACE,
                "includeInterfaceView": True,
                "includeCreatedEventBlob": False,
            }}}
        }]
        entries = self.active_contracts(party, offset, interface_filter)
        holdings: list[Holding] = []
        for item in entries:
            created = (item.get("contractEntry", {})
                       .get("JsActiveContract", {})
                       .get("createdEvent", {}))
            for iv in created.get("interfaceViews", []):
                view = iv.get("viewValue", {})
                lock = view.get("lock")
                instrument_id = view.get("instrumentId", {}) or {}
                holdings.append(Holding(
                    contract_id=created.get("contractId"),
                    amount=view.get("amount"),
                    instrument=instrument_id.get("id"),
                    administrator=instrument_id.get("admin"),
                    locked=lock is not None,
                    lock_expiry=(lock or {}).get("expiresAt") if lock else None,
                ))
        return HoldingsSummary(party=party, offset=offset, holdings=holdings)

    # -- writes / mutations (documented, NEVER called by diagnostics) ------

    def allocate_party(self, hint: str) -> dict:
        """Allocate a party. **Never called by the diagnostic.** Kept so the
        endpoint is documented and covered by a "did not call" test."""
        resp = self._raise_for_status(
            self._post("/v2/parties", {"partyIdHint": hint}), "allocate-party")
        return resp.json()

    def grant_act_as(self, user_id: str, party: str) -> dict:
        """Grant CanActAs. **Explicit opt-in only** - never from diagnostics."""
        body = {"userId": user_id, "identityProviderId": "",
                "rights": [{"kind": {"CanActAs": {"value": {"party": party}}}}]}
        resp = self._raise_for_status(
            self._post(f"/v2/users/{user_id}/rights", body), "grant-act-as")
        return resp.json()

    def submit_and_wait(self, commands: list, act_as, disclosed=None,
                        command_id: str | None = None) -> dict:
        """Submit a command and block until committed.

        **Implemented but NEVER called by the diagnostic layer.** A write must
        be a separate, explicitly-invoked, human-approved action.
        """
        body = {
            "commands": commands,
            "commandId": command_id or f"scandex-{uuid.uuid4()}",
            "actAs": act_as if isinstance(act_as, list) else [act_as],
            "userId": self.config.user,
        }
        if disclosed:
            body["disclosedContracts"] = disclosed
        resp = self._raise_for_status(
            self._post("/v2/commands/submit-and-wait", body), "submit-and-wait")
        return resp.json()
