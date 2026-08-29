"""Scandex Cantor8 (Canton Network) API integration and diagnostics.

A small, dependency-free layer over the Cantor8 DevNet services that Scandex
talks to: Keycloak auth, the Canton JSON Ledger API v2, the token standard
registry, and the scanner / public Scan read APIs.

This package is deliberately separate from the older ``c8lab.py`` scratch tool
at the repository root. ``c8lab.py`` stays a quick manual helper; this package
is the structured, tested layer. Neither imports the other.

Runtime code uses the Python standard library only.
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
