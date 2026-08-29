import unittest

from _fakes import FakeTransport, make_config, token_response, FAKE_JWT

from scandex_api.auth import Authenticator
from scandex_api.errors import AuthError
from scandex_api.http import HttpClient


def _client(transport):
    return HttpClient(timeout=5.0, retries=0, transport=transport)


class AuthTests(unittest.TestCase):
    def test_successful_auth_exposes_safe_claims_only(self):
        t = FakeTransport().add("POST", "/openid-connect/token",
                                json_body=token_response())
        auth = Authenticator(make_config(), _client(t))
        info = auth.token_info()
        self.assertEqual(info.sub, "ledger-api-user")
        self.assertGreater(info.expires_in, 0)
        self.assertEqual(info.token_type, "Bearer")
        # The raw token is never in the safe dict.
        self.assertNotIn(FAKE_JWT, str(info.as_dict()))

    def test_invalid_credentials_401(self):
        t = FakeTransport().add("POST", "/openid-connect/token", status=401,
                                json_body={"error": "invalid_client"})
        auth = Authenticator(make_config(), _client(t))
        with self.assertRaises(AuthError) as ctx:
            auth.bearer()
        self.assertIn("401", str(ctx.exception))

    def test_missing_secret_raises(self):
        auth = Authenticator(make_config(client_secret=""), _client(FakeTransport()))
        with self.assertRaises(AuthError):
            auth.bearer()

    def test_malformed_token_response(self):
        t = FakeTransport().add("POST", "/openid-connect/token",
                                json_body={"not_a_token": True})
        auth = Authenticator(make_config(), _client(t))
        with self.assertRaises(AuthError):
            auth.bearer()

    def test_token_is_cached_second_call_no_http(self):
        t = FakeTransport().add("POST", "/openid-connect/token",
                                json_body=token_response(expires_in=300))
        auth = Authenticator(make_config(), _client(t))
        auth.bearer()
        auth.bearer()
        auth.auth_header()
        self.assertEqual(t.call_count("POST", "/openid-connect/token"), 1)

    def test_token_refreshes_near_expiry(self):
        t = FakeTransport().add("POST", "/openid-connect/token",
                                json_body=token_response(expires_in=40))
        auth = Authenticator(make_config(), _client(t))
        clock = {"t": 1000.0}
        auth._now = lambda: clock["t"]
        auth.bearer()  # fetch 1; expires_at = 1000 + 40 - 30 = 1010
        clock["t"] = 1005.0
        auth.bearer()  # still valid, no refetch
        self.assertEqual(t.call_count("POST", "/openid-connect/token"), 1)
        clock["t"] = 1020.0
        auth.bearer()  # past expiry-skew -> refetch
        self.assertEqual(t.call_count("POST", "/openid-connect/token"), 2)


if __name__ == "__main__":
    unittest.main()
