"""Typed errors for the Scandex Cantor8 client.

Every error carries a plain-English message that is safe to show a beginner.
Messages must never contain a secret or a bearer token; callers that build
messages from live data route them through ``redaction.redact`` first.
"""
from __future__ import annotations


class ScandexError(Exception):
    """Base class for everything this package raises on purpose."""


class ConfigError(ScandexError):
    """Configuration is missing or invalid. The program cannot even start.

    Maps to process exit code 2.
    """


class AuthError(ScandexError):
    """Keycloak rejected the credentials, or the token response was malformed.

    This is a "the service answered but said no" error, distinct from the
    service being unreachable (see ``UnreachableError``).
    """


class UnreachableError(ScandexError):
    """A host could not be reached at all. This is a network/VPN problem,
    not a credentials problem."""


class TimeoutError_(ScandexError):
    """A request did not complete within the configured timeout.

    Named with a trailing underscore so it never shadows the builtin
    ``TimeoutError`` at call sites that also catch OS-level timeouts.
    """


class HttpError(ScandexError):
    """A request completed but returned a non-success HTTP status.

    Carries the status code and (redacted) body so callers can tell a
    401 ("who are you") from a 403 ("not yours / m2m only") from a 405
    ("wrong verb").
    """

    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


class PermissionError_(HttpError):
    """HTTP 403: the token is valid but does not grant rights over this
    resource, or the endpoint is machine-to-machine only."""


class NotAuthenticatedError(HttpError):
    """HTTP 401: no valid credentials were presented."""
