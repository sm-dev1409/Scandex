"""A1 scanner: follow the ledger, cache what this node can see.

Two-phase, on purpose:

1. **Seed** - on first run for a party (no saved offset in ``ledger_offsets``),
   read ``ledger-end`` and then the active contract set at that offset. That
   gives balances *now*. Streaming from the current end only gives you the
   future - balances would read zero until traffic happens. This is trap #1
   in the A1 brief.
2. **Catch up** - on every run after that, poll the transaction-tree stream
   in ``(saved_offset, ledger_end]`` and apply each update. The offset is
   saved as we go, so a kill and restart resumes cleanly with no gap and no
   full replay.

Everything is read-only. The indexer never calls ``submit-and-wait``, party
allocation, or any mutating endpoint. It shares the redaction discipline
used by the diagnostic layer.

Streaming is HTTP POST batch polling of ``/v2/updates/trees`` rather than
the WebSocket variant, so the toolkit stays stdlib-only. See
:meth:`~scandex_api.ledger.LedgerClient.updates` for the tradeoff.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field

from . import redaction
from .db import UPDATES_STREAM, Database
from .ledger import HOLDING_INTERFACE, LedgerClient


def _utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class IndexStats:
    """What one ``run_once`` did, so the CLI can print a one-line summary."""

    seeded_parties: list[str] = field(default_factory=list)
    seeded_holdings: int = 0
    updates_processed: int = 0
    holdings_created: int = 0
    holdings_archived: int = 0
    transfers_recorded: int = 0
    start_offset: str | None = None
    end_offset: str | None = None

    def as_dict(self) -> dict:
        return {
            "seededParties": list(self.seeded_parties),
            "seededHoldings": self.seeded_holdings,
            "updatesProcessed": self.updates_processed,
            "holdingsCreated": self.holdings_created,
            "holdingsArchived": self.holdings_archived,
            "transfersRecorded": self.transfers_recorded,
            "startOffset": self.start_offset,
            "endOffset": self.end_offset,
        }


class Indexer:
    """Follow the ledger for a fixed set of parties into a SQLite database."""

    def __init__(
        self,
        db: Database,
        ledger: LedgerClient,
        parties: list[str],
        logger=None,
    ):
        if not parties:
            raise ValueError("Indexer needs at least one party to follow.")
        self.db = db
        self.ledger = ledger
        self.parties = list(parties)
        self._log = logger or (lambda _msg: None)

    def _emit(self, msg: str) -> None:
        # Route every log line through the same redactor the rest of the
        # package uses; a stray offset or party id is fine, a stray token is
        # not, and we do not want the indexer to be the one that leaks.
        self._log(redaction.redact(msg))

    # -- top level --------------------------------------------------------

    def run_once(self) -> IndexStats:
        """One pass: seed any un-seeded parties, then catch up to ledger-end.

        Safe to call repeatedly. The second call for the same party does
        **not** re-read the ACS - the saved offset in ``ledger_offsets`` is
        the restart guarantee A1 asks for.
        """
        stats = IndexStats()
        saved = self.db.get_offset(UPDATES_STREAM)
        stats.start_offset = saved

        if saved is None:
            offset = self._seed(stats)
            stats.end_offset = offset
            return stats

        self._emit(f"resuming from saved offset {saved}; not re-reading the ACS")
        end = self._catch_up(saved, stats)
        stats.end_offset = end
        return stats

    # -- phase 1: ACS seed ------------------------------------------------

    def _seed(self, stats: IndexStats) -> str:
        """Read ledger-end, then the ACS for every configured party, into the
        ``holdings`` table. Records the ledger-end offset so subsequent runs
        stream forward from there instead of re-reading."""
        offset = self.ledger.ledger_end()
        self._emit(f"seeding ACS at ledger-end offset={offset}")

        for party in self.parties:
            summary = self.ledger.holdings(party, offset=offset)
            for h in summary.holdings:
                self.db.upsert_holding(
                    contract_id=h.contract_id,
                    party_id=party,
                    amount=h.amount,
                    instrument_id=h.instrument,
                    administrator=h.administrator,
                    locked=h.locked,
                    lock_expiry=h.lock_expiry,
                    read_at_offset=summary.offset,
                )
                stats.seeded_holdings += 1
            stats.seeded_parties.append(party)
            self._emit(
                f"seeded party={party.split('::')[0]} "
                f"holdings={len(summary.holdings)} "
                f"total={summary.total} spendable={summary.spendable}"
            )

        self.db.set_offset(offset, stream=UPDATES_STREAM)
        return str(offset)

    # -- phase 2: stream forward -----------------------------------------

    def _catch_up(self, saved_offset: str, stats: IndexStats) -> str:
        """Fetch updates in ``(saved_offset, current_end]`` and apply each one.

        Progress is checkpointed per-update so a crash mid-batch resumes at
        the exact update that failed, never re-reading the ACS.
        """
        current_end = self.ledger.ledger_end()
        if str(current_end) == str(saved_offset):
            self._emit("no new updates since last run")
            return str(current_end)

        begin = _coerce_offset(saved_offset)
        end = _coerce_offset(current_end)
        updates = self.ledger.updates(begin, end, self.parties)
        self._emit(f"fetched {len(updates)} update(s) in ({begin}, {end}]")

        last_seen_offset: str = str(saved_offset)
        for raw in updates:
            tree = _extract_tree(raw)
            if tree is None:
                continue
            update_id = tree.get("updateId") or tree.get("update_id")
            update_offset = tree.get("offset")
            self._apply_tree(tree, update_id, update_offset, stats)
            stats.updates_processed += 1
            if update_offset is not None:
                last_seen_offset = str(update_offset)
                self.db.set_offset(last_seen_offset, stream=UPDATES_STREAM)

        # Even if no update carried an offset (unlikely), record the
        # ledger-end we asked up to so we do not re-fetch the same range.
        self.db.set_offset(str(current_end), stream=UPDATES_STREAM)
        return str(current_end)

    # -- transaction tree walk -------------------------------------------

    def _apply_tree(
        self, tree: dict, update_id: str | None, update_offset,
        stats: IndexStats,
    ) -> None:
        """Walk one transaction tree, apply Holding created/archived events.

        A tree is *not* a flat list (trap #3 in A1). ``eventsById`` holds
        both created and exercised events keyed by node id; ``rootEventIds``
        + each event's ``childEventIds`` describe the parent/child structure.
        For balance and transfer bookkeeping the flat pass over
        ``eventsById`` is enough - we care about which Holdings appeared and
        which disappeared, not the choice-call nesting.
        """
        events = tree.get("eventsById") or tree.get("events_by_id") or {}
        if isinstance(events, list):
            # Some responses ship events as an ordered list of {eventId, ...};
            # normalise so the loop below is uniform.
            events = {str(i): e for i, e in enumerate(events)}

        for _event_id, wrapped in events.items():
            kind, event = _unwrap_event(wrapped)
            if event is None:
                continue

            if kind == "created":
                holding = _holding_from_created(event)
                if holding is None:
                    continue
                owner = holding["owner"] or _first_witness(event)
                if not owner:
                    continue
                contract_id = event.get("contractId") or holding["contract_id"]
                self.db.upsert_holding(
                    contract_id=contract_id,
                    party_id=owner,
                    amount=holding["amount"],
                    instrument_id=holding["instrument"],
                    administrator=holding["administrator"],
                    locked=holding["locked"],
                    lock_expiry=holding["lock_expiry"],
                    read_at_offset=update_offset,
                )
                stats.holdings_created += 1
                if self.db.insert_transfer(
                    update_id=update_id, sender=None, receiver=owner,
                    instrument_id=holding["instrument"], amount=holding["amount"],
                    transfer_kind="credit", status="settled", source="ledger",
                ):
                    stats.transfers_recorded += 1

            elif kind == "exercised":
                if not _is_holding_archive(event):
                    continue
                contract_id = event.get("contractId")
                if not contract_id:
                    continue
                # Look the archived holding up so we know sender/amount/instrument
                row = self.db.conn.execute(
                    "SELECT party_id, amount, instrument_id "
                    "FROM holdings WHERE contract_id = ?",
                    (contract_id,),
                ).fetchone()
                self.db.archive_holding(contract_id, update_offset)
                stats.holdings_archived += 1
                if row is None:
                    continue
                if self.db.insert_transfer(
                    update_id=update_id,
                    sender=row["party_id"], receiver=None,
                    instrument_id=row["instrument_id"], amount=row["amount"],
                    transfer_kind="debit", status="settled", source="ledger",
                ):
                    stats.transfers_recorded += 1

    # -- polling loop (optional; CLI wraps this) --------------------------

    def follow(self, tick_seconds: float = 5.0, max_ticks: int | None = None) -> None:
        """Run ``run_once`` on a loop until interrupted or ``max_ticks``.

        A tiny convenience for a demo / hackathon session. Real deployments
        would call ``run_once`` from a scheduler; this exists so a single
        command tails the ledger without a shell loop.
        """
        i = 0
        while True:
            stats = self.run_once()
            self._emit(
                f"tick offset={stats.end_offset} updates={stats.updates_processed} "
                f"created={stats.holdings_created} archived={stats.holdings_archived}"
            )
            i += 1
            if max_ticks is not None and i >= max_ticks:
                return
            time.sleep(tick_seconds)


# --------------------------------------------------------------------------
#  Tree parsing helpers (pure functions - straight to test)
# --------------------------------------------------------------------------

def _coerce_offset(value):
    """Ledger API v2 offsets are integers in the wire format; the client stores
    them as strings so the DB stays type-agnostic. Coerce back on the way out
    when we hand them to the ledger."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _extract_tree(raw: dict) -> dict | None:
    """The updates response wraps each item in one of a few discriminator
    shapes across Canton versions. Return the inner transaction-tree dict."""
    if not isinstance(raw, dict):
        return None
    for key in ("TransactionTree", "transactionTree", "Transaction", "transaction",
                "update", "Update"):
        inner = raw.get(key)
        if isinstance(inner, dict):
            # Some shapes nest again as {"value": {...}}.
            if "value" in inner and isinstance(inner["value"], dict):
                return inner["value"]
            return inner
    # Already the tree itself.
    if "eventsById" in raw or "events_by_id" in raw or "updateId" in raw:
        return raw
    return None


def _unwrap_event(wrapped) -> tuple[str, dict | None]:
    """Return ``('created'|'exercised'|'', event_dict|None)``."""
    if not isinstance(wrapped, dict):
        return "", None
    for created_key in ("CreatedTreeEvent", "CreatedEvent", "created"):
        node = wrapped.get(created_key)
        if isinstance(node, dict):
            return "created", node.get("value", node)
    for ex_key in ("ExercisedTreeEvent", "ExercisedEvent", "exercised"):
        node = wrapped.get(ex_key)
        if isinstance(node, dict):
            return "exercised", node.get("value", node)
    return "", None


def _holding_from_created(created: dict) -> dict | None:
    """Extract the Holding fields from a created event's interface view.

    Returns ``None`` when the event isn't a Holding (some other created
    contract in the same tree, e.g. a TransferInstruction) - we ignore those
    for balance/transfer bookkeeping.
    """
    for iv in created.get("interfaceViews", []) or []:
        iface_id = (iv.get("interfaceId") or iv.get("interface_id") or "")
        if HOLDING_INTERFACE not in iface_id and "Holding" not in iface_id:
            continue
        view = iv.get("viewValue") or iv.get("view_value") or {}
        lock = view.get("lock")
        instrument = view.get("instrumentId") or view.get("instrument_id") or {}
        return {
            "contract_id": created.get("contractId"),
            "owner": view.get("owner"),
            "amount": view.get("amount"),
            "instrument": instrument.get("id"),
            "administrator": instrument.get("admin"),
            "locked": lock is not None,
            "lock_expiry": (lock or {}).get("expiresAt") if lock else None,
        }
    return None


def _is_holding_archive(exercised: dict) -> bool:
    """True if this exercise consumed a Holding contract."""
    if not exercised.get("consuming", False):
        return False
    iface = exercised.get("interfaceId") or exercised.get("interface_id") or ""
    tmpl = exercised.get("templateId") or exercised.get("template_id") or ""
    return "Holding" in iface or "Holding" in tmpl


def _first_witness(event: dict) -> str | None:
    for key in ("witnessParties", "witness_parties", "signatories", "observers"):
        parties = event.get(key)
        if parties:
            return parties[0]
    return None
