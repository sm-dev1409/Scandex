"""Secret redaction.

Every line this package logs, and every byte it writes into a report, passes
through :func:`redact`. The rule is simple and paranoid: if a string looks like
a secret, a bearer token, a JWT, or the value of a ``*_SECRET`` / ``*_TOKEN``
environment variable, it is replaced with ``***redacted***``.

Redaction is defence in depth. The rest of the package is written so that a raw
secret or token is never placed into a log line or report in the first place;
this module is the backstop that catches mistakes.
"""
from __future__ import annotations

import os
import re

MASK = "***redacted***"

# Literal secret values registered at runtime (e.g. the client secret and any
# live access token). These are masked wherever they appear, verbatim.
_literal_secrets: set[str] = set()

# A JWT: three base64url segments separated by dots, starting with the classic
# "eyJ" header prefix.
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}")

# An HTTP Authorization bearer value.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")

# A "key: value" or "key=value" pair whose key names a secret/token/password.
# Works for JSON ("access_token": "..."), form bodies (client_secret=...), and
# shell-ish assignments (C8_CLIENT_SECRET=...).
_KV_RE = re.compile(
    r'(?i)("?[A-Za-z0-9_.-]*(?:secret|token|password|passwd|api[_-]?key)"?\s*[:=]\s*)'
    r'("?)([^"\s,&}\]]+)(\2)'
)


def register_secret(value: str | None) -> None:
    """Mark a concrete value as a secret to be masked wherever it appears.

    Short values are ignored: masking a 1-3 character "secret" would corrupt
    ordinary output for no security benefit.
    """
    if value and len(value) >= 6:
        _literal_secrets.add(value)


def register_env_secrets(environ: "os._Environ[str] | dict[str, str] | None" = None) -> None:
    """Register the value of every ``*_SECRET`` / ``*_TOKEN`` variable found in
    the given environment (defaults to ``os.environ``)."""
    env = os.environ if environ is None else environ
    for name, value in env.items():
        upper = name.upper()
        if upper.endswith("_SECRET") or upper.endswith("_TOKEN") or upper.endswith("_PASSWORD"):
            register_secret(value)


def redact(text: object) -> str:
    """Return ``text`` (coerced to ``str``) with anything secret-looking masked."""
    s = text if isinstance(text, str) else str(text)

    # 1. Known literal secrets first, so partial matches below cannot leak a
    #    prefix of a registered value.
    for secret in _literal_secrets:
        if secret:
            s = s.replace(secret, MASK)

    # 2. Structural patterns.
    s = _JWT_RE.sub(MASK, s)
    s = _BEARER_RE.sub(lambda m: m.group(1) + MASK, s)
    s = _KV_RE.sub(lambda m: m.group(1) + m.group(2) + MASK + m.group(4), s)
    return s


def redact_headers(headers: dict) -> dict:
    """Return a copy of ``headers`` with Authorization/secret values masked,
    safe for logging."""
    out = {}
    for key, value in (headers or {}).items():
        if key.lower() in ("authorization", "proxy-authorization"):
            out[key] = MASK
        else:
            out[key] = redact(value)
    return out
