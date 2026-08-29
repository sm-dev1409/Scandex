"""Keycloak client-credentials authentication with an in-memory token cache.

Flow (per the Cantor8 DevNet setup)::

    POST {C8_IDP}/realms/master/protocol/openid-connect/token
    Content-Type: application/x-www-form-urlencoded
    grant_type=client_credentials&client_id=hackathon&client_secret=<secret>

The response is validated (``access_token``, ``expires_in``, ``token_type``).
The token is cached in memory and reused until 30 seconds before it expires,
then refreshed. The raw token is never returned to callers, never logged, and
never written to a report - only *safe* claims (``sub``, and ``exp`` as a
countdown) are exposed.
"""
from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass

from . import redaction
from .config import Config
from .errors import AuthError
from .http import HttpClient

# Refresh this many seconds before the token actually expires, so a call never
# goes out with an almost-dead token.
REFRESH_SKEW = 30.0


@dataclass
class TokenInfo:
    """Safe-to-display facts about the current token. Never holds the raw token."""

    sub: str | None
    expires_in: int          # seconds from now until expiry (a countdown)
    token_type: str
    scopes: list[str]

    def as_dict(self) -> dict:
        return {
            "sub": self.sub,
            "expiresInSeconds": self.expires_in,
            "tokenType": self.token_type,
            "scopes": self.scopes,
        }


def _safe_claims(access_token: str) -> tuple[str | None, list[str]]:
    """Decode the JWT payload WITHOUT verifying it, only to surface the ``sub``
    and scopes for display. We never trust these for security decisions; the
    ledger does the trusting. Returns (sub, scopes)."""
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return None, []
        payload_b64 = parts[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        scope = payload.get("scope", "")
        scopes = scope.split() if isinstance(scope, str) else []
        return payload.get("sub"), scopes
    except Exception:
        return None, []


class Authenticator:
    """Issues and caches a Keycloak access token for one :class:`Config`."""

    def __init__(self, config: Config, http: HttpClient | None = None):
        self.config = config
        self.http = http or HttpClient(timeout=config.timeout)
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._info: TokenInfo | None = None
        self._now = time.time  # injectable for tests

    # -- public API -------------------------------------------------------

    def bearer(self, force_refresh: bool = False) -> str:
        """Return a valid raw access token, fetching or refreshing as needed.

        Kept internal in spirit: callers should prefer :meth:`auth_header`.
        """
        if not self.config.has_secret:
            raise AuthError(self.config.missing_secret_message())
        if force_refresh or self._token is None or self._now() >= self._expires_at:
            self._fetch()
        assert self._token is not None
        return self._token

    def auth_header(self, force_refresh: bool = False) -> dict:
        """The ``Authorization`` header dict to attach to an authenticated call."""
        return {"Authorization": f"Bearer {self.bearer(force_refresh)}"}

    def token_info(self, refresh_countdown: bool = True) -> TokenInfo:
        """Return safe token facts, ensuring a token exists first."""
        self.bearer()
        info = self._info
        assert info is not None
        if refresh_countdown:
            remaining = max(0, int(self._expires_at - self._now()))
            info = TokenInfo(info.sub, remaining, info.token_type, info.scopes)
        return info

    @property
    def cached(self) -> bool:
        return self._token is not None and self._now() < self._expires_at

    # -- internals --------------------------------------------------------

    def _fetch(self) -> None:
        resp = self.http.post_form(
            self.config.token_url,
            {
                "grant_type": "client_credentials",
                "client_id": self.config.client_id,
                "client_secret": self.config.client_secret,
            },
        )
        if resp.status == 401:
            raise AuthError(
                "The service answered, but rejected these credentials (HTTP 401). "
                "Check C8_CLIENT_ID and C8_CLIENT_SECRET with the Cantor8 team."
            )
        if not resp.ok:
            raise AuthError(
                redaction.redact(
                    f"Token request failed with HTTP {resp.status}. {resp.text()}"
                )
            )
        payload = resp.json()
        access_token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        token_type = payload.get("token_type")
        if not access_token or not isinstance(access_token, str):
            raise AuthError(
                "Token response was missing 'access_token'. The auth service "
                "answered but not with a token - this usually means the realm "
                "or client configuration is wrong."
            )
        if not isinstance(expires_in, (int, float)):
            raise AuthError("Token response was missing a numeric 'expires_in'.")
        if not token_type:
            raise AuthError("Token response was missing 'token_type'.")

        redaction.register_secret(access_token)
        self._token = access_token
        self._expires_at = self._now() + float(expires_in) - REFRESH_SKEW
        sub, scopes = _safe_claims(access_token)
        self._info = TokenInfo(sub, int(expires_in), str(token_type), scopes)
