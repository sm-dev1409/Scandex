import unittest

from _fakes import FakeTransport, make_config, token_response

from scandex_api.auth import Authenticator
from scandex_api.errors import NotAuthenticatedError, PermissionError_
from scandex_api.http import HttpClient
from scandex_api.ledger import LedgerClient


def _wire(transport):
    cfg = make_config()
    http = HttpClient(timeout=5.0, retries=0, transport=transport)
    auth = Authenticator(cfg, http)
    return cfg, LedgerClient(cfg, auth, http)


def _base_transport():
    return FakeTransport().add("POST", "/openid-connect/token",
                               json_body=token_response())


def _holding_entry(amount, instrument="Amulet", admin="DSO::1", locked=False,
                   cid="cid-1", expires=None):
    lock = {"expiresAt": expires} if locked else None
    return {"contractEntry": {"JsActiveContract": {"createdEvent": {
        "contractId": cid,
        "interfaceViews": [{"viewValue": {
            "amount": amount,
            "instrumentId": {"id": instrument, "admin": admin},
            "lock": lock,
        }}],
    }}}}


class LedgerTests(unittest.TestCase):
    def test_ledger_end_ok(self):
        t = _base_transport().add("GET", "/v2/state/ledger-end",
                                  json_body={"offset": "12345"})
        _, ledger = _wire(t)
        self.assertEqual(ledger.ledger_end(), "12345")

    def test_ledger_401(self):
        t = _base_transport().add("GET", "/v2/state/ledger-end", status=401,
                                  json_body={"error": "unauthenticated"})
        _, ledger = _wire(t)
        with self.assertRaises(NotAuthenticatedError):
            ledger.ledger_end()

    def test_ledger_403(self):
        t = _base_transport().add("POST", "/v2/state/active-contracts", status=403,
                                  json_body={"error": "permission"})
        _, ledger = _wire(t)
        with self.assertRaises(PermissionError_):
            ledger.active_contracts("someparty", "10")

    def test_parties_local_vs_remote(self):
        t = _base_transport().add("GET", "/v2/parties", json_body={"partyDetails": [
            {"party": "alice::1", "isLocal": True},
            {"party": "bob::2", "isLocal": False},
        ]})
        _, ledger = _wire(t)
        parties = ledger.parties()
        self.assertEqual(len(parties), 2)
        local = ledger.local_parties()
        self.assertEqual([p.party for p in local], ["alice::1"])
        self.assertEqual(ledger.find_party("bob").is_local, False)
        self.assertIsNone(ledger.find_party("nobody"))

    def test_active_contracts_uses_offset_and_interface_filter(self):
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": "99"})
             .add("POST", "/v2/state/active-contracts", json_body=[
                 _holding_entry("100.0")]))
        _, ledger = _wire(t)
        summary = ledger.holdings("alice::1")
        self.assertEqual(summary.offset, "99")
        # The request body must carry an InterfaceFilter at the read offset.
        body = t.bodies_sent("/v2/state/active-contracts")[0]
        self.assertEqual(body["activeAtOffset"], "99")
        cumulative = body["filter"]["filtersByParty"]["alice::1"]["cumulative"]
        self.assertIn("InterfaceFilter", cumulative[0]["identifierFilter"])

    def test_holdings_parsed_and_spendable_excludes_locked(self):
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": "5"})
             .add("POST", "/v2/state/active-contracts", json_body=[
                 _holding_entry("100.0", cid="a", locked=False),
                 _holding_entry("40.0", cid="b", locked=True, expires="2030-01-01T00:00:00Z"),
             ]))
        _, ledger = _wire(t)
        summary = ledger.holdings("alice::1", offset="5")
        self.assertEqual(summary.total, 140.0)
        self.assertEqual(summary.spendable, 100.0)
        self.assertEqual(summary.non_spendable, 40.0)
        self.assertEqual(summary.locked_count, 1)
        locked = [h for h in summary.holdings if h.locked][0]
        self.assertEqual(locked.lock_expiry, "2030-01-01T00:00:00Z")


if __name__ == "__main__":
    unittest.main()
