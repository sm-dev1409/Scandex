"""Offline test doubles. No test in this suite ever touches the network.

Everything is mocked at the HTTP *transport* boundary — the single callable

    transport(method, url, headers, data, timeout) -> (status, body_bytes, headers)

that :class:`scandex_api.http.HttpClient` calls. We never monkeypatch urllib
globally; we inject a fake transport instead.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Make the package importable when tests run from a bare clone.
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from scandex_api.errors import TimeoutError_, UnreachableError  # noqa: E402


class Route:
    def __init__(self, method, url_contains, status=200, json_body=None,
                 body=None, raise_exc=None):
        self.method = method.upper()
        self.url_contains = url_contains
        self.status = status
        self.raise_exc = raise_exc
        if body is not None:
            self.body = body if isinstance(body, bytes) else body.encode()
        elif json_body is not None:
            self.body = json.dumps(json_body).encode()
        else:
            self.body = b""

    def matches(self, method, url):
        return method.upper() == self.method and self.url_contains in url


class FakeTransport:
    """Records every call and answers from a list of routes (first match wins)."""

    def __init__(self):
        self.routes: list[Route] = []
        self.calls: list[dict] = []

    def add(self, method, url_contains, **kwargs) -> "FakeTransport":
        self.routes.append(Route(method, url_contains, **kwargs))
        return self

    def add_timeout(self, method, url_contains):
        return self.add(method, url_contains, raise_exc="timeout")

    def add_unreachable(self, method, url_contains):
        return self.add(method, url_contains, raise_exc="unreachable")

    def __call__(self, method, url, headers, data, timeout):
        self.calls.append({"method": method, "url": url, "headers": dict(headers),
                           "data": data, "timeout": timeout})
        for route in self.routes:
            if route.matches(method, url):
                if route.raise_exc == "timeout":
                    raise TimeoutError_(f"timed out calling {url}")
                if route.raise_exc == "unreachable":
                    raise UnreachableError(f"could not reach {url}")
                return route.status, route.body, {}
        return 404, json.dumps({"error": f"no route for {method} {url}"}).encode(), {}

    # -- assertions used by tests -----------------------------------------

    def called(self, method, url_contains) -> bool:
        return any(c["method"].upper() == method.upper() and url_contains in c["url"]
                   for c in self.calls)

    def call_count(self, method=None, url_contains=None) -> int:
        n = 0
        for c in self.calls:
            if method and c["method"].upper() != method.upper():
                continue
            if url_contains and url_contains not in c["url"]:
                continue
            n += 1
        return n

    def bodies_sent(self, url_contains):
        out = []
        for c in self.calls:
            if url_contains in c["url"] and c["data"]:
                try:
                    out.append(json.loads(c["data"]))
                except Exception:
                    out.append(c["data"].decode(errors="replace"))
        return out


def make_config(**overrides):
    """A Config populated with harmless test values."""
    from scandex_api.config import Config
    base = dict(
        base="https://ledger.test/api/ledger",
        idp="https://auth.test",
        client_id="hackathon",
        client_secret="test-secret-value",
        registry="https://registry.test",
        user="ledger-api-user",
        scanner_base="https://scanner.test",
        scan_base="https://scan.test",
        party=None,
        admin_party=None,
        registry_host=None,
        timeout=5.0,
    )
    base.update(overrides)
    return Config(**base)


# A syntactically valid-looking JWT (header.payload.signature) whose payload
# decodes to {"sub":"ledger-api-user","exp":9999999999,"scope":"openid"}.
FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJzdWIiOiJsZWRnZXItYXBpLXVzZXIiLCJleHAiOjk5OTk5OTk5OTksInNjb3BlIjoib3BlbmlkIn0."
    "c2lnbmF0dXJlLXBsYWNlaG9sZGVy"
)


def token_response(access_token=FAKE_JWT, expires_in=300, token_type="Bearer"):
    return {"access_token": access_token, "expires_in": expires_in,
            "token_type": token_type}
