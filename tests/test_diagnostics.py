import json
import unittest

from _fakes import FakeTransport, make_config, token_response, FAKE_JWT

from scandex_api import redaction
from scandex_api.diagnostics import Diagnostics
from scandex_api.http import HttpClient
from scandex_api.models import Outcome


def _full_transport(transfer_kind="direct", scanner_delay=10.0):
    """A transport wired for a complete, healthy DevNet-like run."""
    t = FakeTransport()
    t.add("POST", "/openid-connect/token", json_body=token_response())
    t.add("GET", "/v2/state/ledger-end", json_body={"offset": "1000"})
    t.add("GET", "/v2/parties", json_body={"partyDetails": [
        {"party": "alice::1", "isLocal": True},
        {"party": "bob::2", "isLocal": False},
    ]})
    t.add("POST", "/v2/state/active-contracts", json_body=[
        {"contractEntry": {"JsActiveContract": {"createdEvent": {
            "contractId": "cid-1",
            "interfaceViews": [{"viewValue": {
                "amount": "100.0",
                "instrumentId": {"id": "Amulet", "admin": "DSO::1"},
                "lock": None}}]}}}},
    ])
    t.add("GET", "/registry/metadata/v1/info", json_body={"adminId": "DSO::1"})
    t.add("GET", "/registry/metadata/v1/instruments", json_body={"instruments": [
        {"id": "Amulet", "name": "Canton Coin", "admin": "DSO::1", "decimals": 10}]})
    t.add("POST", "/transfer-instruction/v1/transfer-factory", json_body={
        "factoryId": "f1", "transferKind": transfer_kind, "choiceContext": {}})
    t.add("GET", "/health", json_body={
        "status": "ok", "db": {"status": "ok", "scannerDelaySecs": scanner_delay}})
    t.add("GET", "/tokens/balance/", status=403, json_body={"error": "not yours"})
    t.add("GET", "/api/scan/v0/splice-instance-names",
          json_body={"networkName": "TestNet"})
    return t


def _diag(transport, **cfg):
    cfg.setdefault("party", "alice::1")
    cfg.setdefault("admin_party", "DSO::1")
    http = HttpClient(timeout=5.0, retries=0, transport=transport)
    return Diagnostics(make_config(**cfg), http=http)


class DiagnosticsRunTests(unittest.TestCase):
    def test_full_run_passes_core_checks(self):
        diag = _diag(_full_transport())
        results = diag.run()
        outcomes = {(r.service, r.endpoint): r.outcome for r in results}
        self.assertEqual(outcomes[("Auth",
            "/realms/master/protocol/openid-connect/token")], Outcome.PASS)
        self.assertEqual(outcomes[("Ledger", "/v2/state/ledger-end")], Outcome.PASS)
        counts = Diagnostics.counts(results)
        self.assertGreaterEqual(counts["passed"], 4)
        self.assertGreaterEqual(counts["manual"], 1)
        self.assertEqual(Diagnostics.exit_code(results), 0)

    def test_run_never_calls_write_endpoints(self):
        t = _full_transport()
        diag = _diag(t)
        diag.run()
        # The two forbidden write paths must never be hit.
        self.assertEqual(t.call_count("POST", "/v2/commands/submit-and-wait"), 0)
        # POST /v2/parties (allocate). Note: GET /v2/parties is allowed.
        self.assertFalse(any(c["method"] == "POST" and c["url"].endswith("/v2/parties")
                             for c in t.calls))
        self.assertEqual(t.call_count("POST", "/v2/users/"), 0)

    def test_no_token_skips_ledger_but_not_public(self):
        t = _full_transport()
        diag = _diag(t, client_secret="")  # no secret -> auth fails
        results = diag.run()
        auth = [r for r in results if r.service == "Auth"][0]
        self.assertEqual(auth.outcome, Outcome.FAIL)
        # Ledger *read* checks are skipped (the "Ledger" service also carries
        # MANUAL_ACTIONS entries, which are EXPECTED MANUAL ACTION, not skipped).
        ledger_reads = [r for r in results
                        if r.service == "Ledger" and r.outcome != Outcome.MANUAL]
        self.assertTrue(ledger_reads)
        self.assertTrue(all(r.outcome == Outcome.SKIPPED for r in ledger_reads))
        # Public registry/scan still attempted.
        self.assertTrue(any(r.service == "Registry" for r in results))

    def test_network_timeout_is_a_clean_fail(self):
        t = _full_transport()
        # Override ledger-end to time out (last matching route still first-wins,
        # so build fresh with the timeout route added before others is complex;
        # instead use a transport where ledger-end times out).
        t2 = FakeTransport()
        t2.add("POST", "/openid-connect/token", json_body=token_response())
        t2.add_timeout("GET", "/v2/state/ledger-end")
        t2.add("GET", "/v2/parties", json_body={"partyDetails": []})
        t2.add("GET", "/registry/metadata/v1/info", json_body={"adminId": "x"})
        t2.add("GET", "/registry/metadata/v1/instruments", json_body={"instruments": []})
        t2.add("GET", "/health", json_body={"status": "ok", "db": {"status": "ok"}})
        t2.add("GET", "/tokens/balance/", status=401, json_body={})
        t2.add("GET", "/api/scan/v0/splice-instance-names", json_body={"networkName": "N"})
        diag = _diag(t2)
        results = diag.run()
        le = [r for r in results if r.endpoint == "/v2/state/ledger-end"][0]
        self.assertEqual(le.outcome, Outcome.FAIL)
        self.assertIn("Timed out", le.summary)
        self.assertEqual(Diagnostics.exit_code(results), 1)

    def test_malformed_json_does_not_crash(self):
        t = _full_transport()
        t2 = FakeTransport()
        t2.add("POST", "/openid-connect/token", json_body=token_response())
        t2.add("GET", "/v2/state/ledger-end", json_body={"offset": "1"})
        t2.add("GET", "/v2/parties", json_body={"partyDetails": [
            {"party": "alice::1", "isLocal": True}]})
        t2.add("POST", "/v2/state/active-contracts", body="<html>not json</html>")
        t2.add("GET", "/registry/metadata/v1/info", body="<html>nope</html>")
        t2.add("GET", "/registry/metadata/v1/instruments", json_body={"instruments": []})
        t2.add("GET", "/health", json_body={"status": "ok", "db": {"status": "ok"}})
        t2.add("GET", "/tokens/balance/", status=401, json_body={})
        t2.add("GET", "/api/scan/v0/splice-instance-names", json_body={"networkName": "N"})
        diag = _diag(t2)
        results = diag.run()  # must not raise
        self.assertTrue(len(results) > 0)


class TransferPreviewTests(unittest.TestCase):
    def test_direct_preview(self):
        diag = _diag(_full_transport(transfer_kind="direct"))
        preview = diag.preview_transfer("alice::1", "bob::2", "25", "Amulet")
        self.assertEqual(preview.transfer_kind, "direct")
        self.assertTrue(preview.receiver_preapproved)
        self.assertEqual(preview.available, 100.0)
        self.assertEqual(preview.spendable_after_locks, 100.0)
        self.assertFalse(preview.as_dict()["submitted"])

    def test_offer_preview(self):
        diag = _diag(_full_transport(transfer_kind="offer"))
        preview = diag.preview_transfer("alice::1", "bob::2", "25", "Amulet")
        self.assertEqual(preview.transfer_kind, "offer")
        self.assertFalse(preview.receiver_preapproved)
        self.assertIn("accept", preview.next_step.lower())

    def test_self_transfer(self):
        diag = _diag(_full_transport())
        preview = diag.preview_transfer("alice::1", "alice::1", "1", "Amulet")
        self.assertEqual(preview.transfer_kind, "self")


class RedactionTests(unittest.TestCase):
    def test_secret_and_jwt_never_survive_in_logs_or_report(self):
        secret = "supersecret-abc-123456"
        redaction.register_secret(secret)
        logs = []
        t = _full_transport()
        http = HttpClient(timeout=5.0, retries=0, transport=t,
                          logger=lambda m: logs.append(m))
        diag = Diagnostics(make_config(client_secret=secret, party="alice::1",
                                       admin_party="DSO::1"), http=http)
        diag.run()
        blob = "\n".join(logs)
        self.assertNotIn(secret, blob)
        self.assertNotIn(FAKE_JWT, blob)
        # And the serialized report:
        report_json = json.dumps(diag.report_dict())
        report_json = redaction.redact(report_json)
        self.assertNotIn(secret, report_json)
        self.assertNotIn(FAKE_JWT, report_json)

    def test_redact_masks_shapes(self):
        self.assertNotIn("eyJ", redaction.redact(
            "Authorization: Bearer " + FAKE_JWT))
        masked = redaction.redact('"client_secret":"hunter2xyz"')
        self.assertNotIn("hunter2xyz", masked)
        self.assertIn("***redacted***", masked)


if __name__ == "__main__":
    unittest.main()
