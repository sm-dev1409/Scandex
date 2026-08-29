"""Plain dataclasses for the domain objects Scandex reads from Cantor8.

These are intentionally dumb data holders - parsing lives in the service
clients, formatting lives in diagnostics. Keeping them separate is what lets a
test check "did we parse a locked holding correctly" without any HTTP.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    """The four verdicts every diagnostic check reports."""

    PASS = "PASS"
    FAIL = "FAIL"
    SKIPPED = "SKIPPED"
    MANUAL = "EXPECTED MANUAL ACTION"


class Importance(str, Enum):
    """How much the Scandex demo needs a given endpoint."""

    REQUIRED = "required"
    USEFUL = "useful"
    OPTIONAL = "optional"


@dataclass
class Party:
    party: str
    is_local: bool
    display_name: str | None = None

    @property
    def hint(self) -> str:
        return self.party.split("::")[0]


@dataclass
class Instrument:
    id: str
    name: str | None
    administrator: str | None
    decimals: int | None
    raw: dict = field(default_factory=dict)


@dataclass
class Holding:
    contract_id: str | None
    amount: str | None
    instrument: str | None
    administrator: str | None
    locked: bool
    lock_expiry: str | None = None

    @property
    def amount_float(self) -> float:
        try:
            return float(self.amount)
        except (TypeError, ValueError):
            return 0.0


@dataclass
class HoldingsSummary:
    """A consistent snapshot of a party's holdings, read at one ledger offset."""

    party: str
    offset: str
    holdings: list[Holding] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(h.amount_float for h in self.holdings)

    @property
    def spendable(self) -> float:
        return sum(h.amount_float for h in self.holdings if not h.locked)

    @property
    def non_spendable(self) -> float:
        return sum(h.amount_float for h in self.holdings if h.locked)

    @property
    def locked_count(self) -> int:
        return sum(1 for h in self.holdings if h.locked)

    def by_instrument(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for h in self.holdings:
            key = h.instrument or "?"
            bucket = out.setdefault(key, {"total": 0.0, "spendable": 0.0, "count": 0})
            bucket["total"] += h.amount_float
            bucket["count"] += 1
            if not h.locked:
                bucket["spendable"] += h.amount_float
        return out


@dataclass
class CheckResult:
    """One diagnostic check. Serializes to the exact field set the brief asks
    for, in both human and JSON output."""

    service: str
    method: str            # HTTP method, or "-" for a non-HTTP note
    endpoint: str
    auth_required: bool
    outcome: Outcome
    summary: str           # short human-readable result
    meaning: str           # one plain-English sentence: what it means
    importance: Importance
    status_code: int | None = None
    latency_ms: float | None = None

    def as_dict(self) -> dict:
        return {
            "service": self.service,
            "method": self.method,
            "endpoint": self.endpoint,
            "authRequired": self.auth_required,
            "statusCode": self.status_code,
            "outcome": self.outcome.value,
            "summary": self.summary,
            "meaning": self.meaning,
            "demoImportance": self.importance.value,
            "latencyMs": round(self.latency_ms, 1) if self.latency_ms is not None else None,
        }


@dataclass
class TransferPreview:
    """The result of a dry-run transfer analysis. Nothing here was submitted."""

    sender: str
    receiver: str
    instrument: str
    amount: float
    available: float
    spendable_after_locks: float
    has_locked_holdings: bool
    receiver_preapproved: bool | None   # None = could not determine
    transfer_kind: str                  # direct | offer | self | unknown
    next_step: str
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "sender": self.sender,
            "receiver": self.receiver,
            "instrument": self.instrument,
            "amount": self.amount,
            "available": self.available,
            "spendableAfterLocks": self.spendable_after_locks,
            "hasLockedHoldings": self.has_locked_holdings,
            "receiverPreapproved": self.receiver_preapproved,
            "transferKind": self.transfer_kind,
            "nextStep": self.next_step,
            "notes": self.notes,
            "submitted": False,
        }
