import unittest

from _fakes import FakeTransport, make_config, token_response

from scandex_api.auth import Authenticator
from scandex_api.http import HttpClient
from scandex_api.scanner import ScannerClient


def _wire(transport):
    cfg = make_config()
    http = HttpClient(timeout=5.0, retries=0, transport=transport)
    auth = Authenticator(cfg, http)
    return cfg, ScannerClient(cfg, auth, http)


class ScannerTests(unittest.TestCase):
    def test_health_ok_with_delay(self):
        t = FakeTransport().add("GET", "/health", json_body={
            "status": "ok", "db": {"status": "ok", "scannerDelaySecs": 10.8}})
        _, scanner = _wire(t)
        resp = scanner.health()
        self.assertTrue(resp.ok)
        self.assertEqual(resp.json()["db"]["scannerDelaySecs"], 10.8)

    def test_interpret_auth_status(self):
        self.assertIn("who are you", ScannerClient.interpret_auth_status(401))
        self.assertIn("permission", ScannerClient.interpret_auth_status(403))
        self.assertIn("wrong", ScannerClient.interpret_auth_status(405).lower())

    def test_balance_needs_auth_token_attached(self):
        t = (FakeTransport()
             .add("POST", "/openid-connect/token", json_body=token_response())
             .add("GET", "/tokens/balance/", status=403,
                  json_body={"error": "not yours"}))
        _, scanner = _wire(t)
        resp = scanner.balance("alice::1")
        self.assertEqual(resp.status, 403)
        # Authorization header must have been attached.
        bal_call = [c for c in t.calls if "/tokens/balance/" in c["url"]][0]
        self.assertIn("Authorization", bal_call["headers"])

    def test_delay_warning_threshold_via_diagnostics(self):
        # A large scannerDelaySecs should surface a WARNING in the check summary.
        from scandex_api.diagnostics import Diagnostics
        t = (FakeTransport()
             .add("POST", "/openid-connect/token", json_body=token_response())
             .add("GET", "/health", json_body={
                 "status": "ok", "db": {"status": "ok", "scannerDelaySecs": 500}})
             .add("GET", "/tokens/balance/", status=401, json_body={})
             .add("GET", "/api/scan/v0/splice-instance-names",
                  json_body={"networkName": "TestNet"}))
        cfg = make_config()
        http = HttpClient(timeout=5.0, retries=0, transport=t)
        diag = Diagnostics(cfg, http=http)
        diag.check_scanner()
        health = [r for r in diag.results if r.endpoint == "/health"][0]
        self.assertIn("WARNING", health.summary)


if __name__ == "__main__":
    unittest.main()
