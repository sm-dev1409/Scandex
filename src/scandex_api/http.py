"""HTTP transport: a thin urllib wrapper with timeouts, retries, JSON handling,
status capture, and redaction.

This module knows nothing about Cantor8. It does one job: turn a request into a
:class:`Response`, or raise a typed error. Authentication, per-service logic and
report formatting live elsewhere.

Tests inject a fake ``transport`` callable, so no test ever touches the network.
The transport boundary is a single function:

    transport(method, url, headers, data, timeout) -> (status, body_bytes, headers)

The default transport uses ``urllib.request``. It never raises on an HTTP error
status (4xx/5xx come back as a normal tuple); it raises only when the host
cannot be reached or the request times out.
"""
from __future__ import annotations

import json
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import redaction
from .errors import HttpError, TimeoutError_, UnreachableError

Transport = Callable[[str, str, dict, "bytes | None", float], "tuple[int, bytes, dict]"]


@dataclass
class Response:
    """A completed HTTP response (any status)."""

    status: int
    body: bytes
    headers: dict
    url: str
    method: str
    latency_ms: float

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        """Parse the body as JSON, or return ``{"raw": <text>}`` if it is not
        JSON (so a stray HTML error page never crashes a caller)."""
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {"raw": redaction.redact(self.body.decode("utf-8", "replace")[:600])}

    def text(self, limit: int = 600) -> str:
        return redaction.redact(self.body.decode("utf-8", "replace")[:limit])


def _default_transport(method, url, headers, data, timeout):
    """Real network transport. Returns (status, body, headers); raises only on
    genuine conn(unreachable) or timeout failures."""
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.getcode(), resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        # A response with an error status is still a response.
        body = b""
        try:
            body = e.read()
        except Exception:  # pragma: no cover - defensive
            pass
        return e.code, body, dict(e.headers or {})
    except (TimeoutError, ssl.SSLError) as e:  # includes socket timeout
        raise TimeoutError_(redaction.redact(f"timed out calling {url}: {e}"))
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError):
            raise TimeoutError_(redaction.redact(f"timed out calling {url}"))
        raise UnreachableError(redaction.redact(f"could not reach {url}: {reason}"))
    except OSError as e:  # pragma: no cover - defensive
        raise UnreachableError(redaction.redact(f"network error calling {url}: {e}"))


class HttpClient:
    """Reusable client. Retries idempotent-ish failures (network, timeout, 5xx)
    a small number of times with linear backoff; never retries a 4xx."""

    def __init__(
        self,
        timeout: float = 30.0,
        retries: int = 2,
        backoff: float = 0.4,
        transport: Transport | None = None,
        logger: "Callable[[str], None] | None" = None,
    ):
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._transport = transport or _default_transport
        self._logger = logger
        self._sleep = time.sleep

    def _log(self, message: str) -> None:
        if self._logger:
            self._logger(redaction.redact(message))

    def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        json_body=None,
        form_body: dict | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Perform a request and return a :class:`Response`.

        Provide at most one of ``json_body`` / ``form_body``. Raises
        :class:`UnreachableError` or :class:`TimeoutError_` on transport
        failure; an HTTP error *status* is returned, not raised.
        """
        headers = dict(headers or {})
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif form_body is not None:
            import urllib.parse
            data = urllib.parse.urlencode(form_body).encode("utf-8")
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        headers.setdefault("Accept", "application/json")

        eff_timeout = self.timeout if timeout is None else timeout
        self._log(f"{method} {url} headers={redaction.redact_headers(headers)}")

        last_error: Exception | None = None
        attempts = self.retries + 1
        for attempt in range(attempts):
            started = time.monotonic()
            try:
                status, body, resp_headers = self._transport(
                    method, url, headers, data, eff_timeout
                )
            except (UnreachableError, TimeoutError_) as e:
                last_error = e
                if attempt < attempts - 1:
                    self._sleep(self.backoff * (attempt + 1))
                    continue
                raise
            latency_ms = (time.monotonic() - started) * 1000.0
            response = Response(status, body, resp_headers, url, method, latency_ms)
            # Retry transient server errors only.
            if status >= 500 and attempt < attempts - 1:
                last_error = HttpError(f"HTTP {status} from {url}", status, response.text())
                self._sleep(self.backoff * (attempt + 1))
                continue
            self._log(f"-> {status} in {latency_ms:.0f} ms")
            return response

        # Unreachable in practice; the loop returns or raises.
        raise last_error or UnreachableError(f"request to {url} failed")

    def get(self, url, headers=None, timeout=None) -> Response:
        return self.request("GET", url, headers=headers, timeout=timeout)

    def post_json(self, url, json_body, headers=None, timeout=None) -> Response:
        return self.request("POST", url, headers=headers, json_body=json_body, timeout=timeout)

    def post_form(self, url, form_body, headers=None, timeout=None) -> Response:
        return self.request("POST", url, headers=headers, form_body=form_body, timeout=timeout)
