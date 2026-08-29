"""Configuration: read and validate the environment, produce a frozen config.

Reads real environment variables, optionally overlaying a local ``.env`` file
(real environment variables win). Produces a frozen :class:`Config`. Error
messages are plain English and distinguish *missing configuration* from
*unreachable service* from *rejected credentials* (the latter two are raised by
the auth/http layers, not here).

Standard library only. The ``.env`` parser is intentionally tiny - no
dependency on ``python-dotenv``.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import redaction
from .errors import ConfigError

# Sensible DevNet defaults, matching API.md / the brief. Only C8_CLIENT_SECRET
# has no default: it must be supplied per session and never written down.
DEFAULTS = {
    "C8_BASE": "https://api.validator.dev.digik.cantor8.tech/api/ledger",
    "C8_IDP": "https://auth.dev.digik.cantor8.tech",
    "C8_CLIENT_ID": "hackathon",
    "C8_REGISTRY": "https://sv-proxy.dev.digik.cantor8.tech",
    "C8_USER": "ledger-api-user",
    "C8_SCANNER_BASE": "https://scanner-ledger-read-api.dev.digik.cantor8.tech",
    "C8_SCAN_BASE": "https://sv-proxy.dev.digik.cantor8.tech",
}


def parse_env_file(path: str | os.PathLike) -> dict[str, str]:
    """Parse a minimal ``.env`` file into a dict.

    Supports ``KEY=value``, ``export KEY=value``, ``#`` comments, blank lines,
    and single/double quoted values. Unknown or malformed lines are skipped
    quietly - this is a convenience loader, not a validator.
    """
    result: dict[str, str] = {}
    p = Path(path)
    if not p.is_file():
        return result
    for raw in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if (len(value) >= 2) and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            result[key] = value
    return result


def _resolve(environ: dict[str, str], env_file: dict[str, str], name: str) -> str | None:
    """Real environment wins, then the .env file, then the built-in default."""
    if name in environ and environ[name] != "":
        return environ[name]
    if name in env_file and env_file[name] != "":
        return env_file[name]
    return DEFAULTS.get(name)


@dataclass(frozen=True)
class Config:
    """Immutable, validated configuration for one run."""

    base: str                       # C8_BASE   - Ledger API base
    idp: str                        # C8_IDP    - Keycloak base
    client_id: str                  # C8_CLIENT_ID
    client_secret: str              # C8_CLIENT_SECRET (never logged)
    registry: str                   # C8_REGISTRY
    user: str                       # C8_USER   - ledger API user id (token sub)
    scanner_base: str               # C8_SCANNER_BASE
    scan_base: str                  # C8_SCAN_BASE  (public, no auth)
    party: str | None = None        # C8_PARTY
    admin_party: str | None = None  # C8_ADMIN_PARTY
    registry_host: str | None = None  # C8_REGISTRY_HOST (Host header override)
    timeout: float = 30.0

    @property
    def token_url(self) -> str:
        return f"{self.idp.rstrip('/')}/realms/master/protocol/openid-connect/token"

    @property
    def has_secret(self) -> bool:
        return bool(self.client_secret)

    def missing_secret_message(self) -> str:
        return (
            "C8_CLIENT_SECRET is not set. Ask the Cantor8 team for the secret "
            "and set it in your shell for this session only "
            "(PowerShell: $env:C8_CLIENT_SECRET = \"<secret>\")."
        )


def load_config(
    environ: dict[str, str] | None = None,
    env_file_path: str | os.PathLike | None = ".env",
    timeout: float | None = None,
    party: str | None = None,
) -> Config:
    """Build a :class:`Config` from the environment and an optional ``.env``.

    ``environ`` defaults to ``os.environ``. ``party`` and ``timeout`` override
    the environment when given (used by CLI flags). Registers every discovered
    secret with the redaction layer before returning.

    Raises :class:`ConfigError` only for structurally missing *non-secret*
    configuration (the base URLs). A missing ``C8_CLIENT_SECRET`` is *not* a
    hard error here - the diagnostic reports it as a failed check rather than
    crashing, which is more useful to a beginner.
    """
    environ = dict(os.environ if environ is None else environ)
    env_file = parse_env_file(env_file_path) if env_file_path else {}

    def get(name: str) -> str | None:
        return _resolve(environ, env_file, name)

    base = get("C8_BASE")
    idp = get("C8_IDP")
    client_id = get("C8_CLIENT_ID")
    registry = get("C8_REGISTRY")
    scanner_base = get("C8_SCANNER_BASE")
    scan_base = get("C8_SCAN_BASE")
    user = get("C8_USER") or "ledger-api-user"
    client_secret = get("C8_CLIENT_SECRET") or ""

    # These have defaults, so a genuine miss means someone set them to empty.
    required_nonsecret = {
        "C8_BASE": base,
        "C8_IDP": idp,
        "C8_CLIENT_ID": client_id,
        "C8_REGISTRY": registry,
        "C8_SCANNER_BASE": scanner_base,
        "C8_SCAN_BASE": scan_base,
    }
    missing = [name for name, value in required_nonsecret.items() if not value]
    if missing:
        raise ConfigError(
            "These required settings are empty: " + ", ".join(missing) + ". "
            "They normally have safe DevNet defaults, so an empty value means "
            "one was set to an empty string. Unset it or give it a real value."
        )

    env_timeout = get("C8_TIMEOUT")
    if timeout is None and env_timeout:
        try:
            timeout = float(env_timeout)
        except ValueError:
            raise ConfigError(f"C8_TIMEOUT must be a number, got {env_timeout!r}.")

    cfg = Config(
        base=base.rstrip("/"),
        idp=idp.rstrip("/"),
        client_id=client_id,
        client_secret=client_secret,
        registry=registry.rstrip("/"),
        user=user,
        scanner_base=scanner_base.rstrip("/"),
        scan_base=scan_base.rstrip("/"),
        party=party or get("C8_PARTY"),
        admin_party=get("C8_ADMIN_PARTY"),
        registry_host=get("C8_REGISTRY_HOST"),
        timeout=timeout if timeout is not None else 30.0,
    )

    # Register secrets so redaction masks them everywhere from here on.
    redaction.register_env_secrets(environ)
    redaction.register_env_secrets(env_file)
    redaction.register_secret(cfg.client_secret)
    return cfg
