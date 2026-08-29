"""A1 scanner: follow the ledger, cache what this node can see.

Two-phase, on purpose:

1. **Seed** - on first run (no saved offset in the ``checkpoint`` table), read
   ``ledger-end`` and then the active contract set at that offset. That gives
   balances *now*. Streaming from the current end only gives you the future -
   balances would read zero until traffic happens. This is trap #1 in the A1
   brief.
2. **Catch up** - on every run after that, poll the transaction-tree stream in
   ``(saved_offset, ledger_end]`` and apply each update. The offset is saved as
   we go, so a kill and restart resumes cleanly with no gap and no full replay.

Persistence goes through :class:`~scandex_api.store.ScannerDB` — the one
canonical database class (``save_holding`` / ``save_event`` /
``archive_holding`` / ``save_transfer`` / ``save_party`` / ``save_offset``).

Everything is read-only. The indexer never calls ``submit-and-wait``, party
allocation, or any mutating endpoint. It shares the redaction discipline used by
the diagnostic layer.

Streaming is HTTP POST batch polling of ``/v2/updates/trees`` rather than the
WebSocket variant, so the toolkit stays stdlib-only.
"""
from __future__ import annotations

import datetime
import time
from dataclasses import dataclass, field

from . import redaction
from .errors import ScandexError
from .ledger import (
    HOLDING_INTERFACE,
    TRANSFER_INSTRUCTION_INTERFACE,
    LedgerClient,
)
from .store import ScannerDB


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
    offers_created: int = 0
    offers_resolved: int = 0
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
            "offersCreated": self.offers_created,
            "offersResolved": self.offers_resolved,
            "startOffset": self.start_offset,
            "endOffset": self.end_offset,
        }


class Indexer:
    """Follow the ledger for a fixed set of parties into a :class:`ScannerDB`."""

    def __init__(
        self,
        db: ScannerDB,
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
        # party_id -> (display_name, is_local), filled from /v2/parties at seed.
        self._party_meta: dict[str, tuple[str | None, bool]] = {}

    def _emit(self, msg: str) -> None:
        # Route every log line through the same redactor the rest of the
        # package uses; a stray offset or party id is fine, a stray token is
        # not, and we do not want the indexer to be the one that leaks.
        self._log(redaction.redact(msg))

    # -- top level --------------------------------------------------------

    def run_once(self) -> IndexStats:
        """One pass: seed on first run, else catch up to ledger-end.

        Safe to call repeatedly. A second call does **not** re-read the ACS -
        the saved offset in ``checkpoint`` is the restart guarantee A1 asks for.
        """
        stats = IndexStats()
        saved = self.db.get_offset()
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

    def _load_party_meta(self) -> None:
        """Best-effort ``/v2/parties`` read so seeded parties get accurate
        ``display_name`` / ``is_local`` instead of a guess. Never fatal: if the
        call fails we fall back to defaults."""
        try:
            for p in self.ledger.parties():
                self._party_meta[p.party] = (p.display_name, p.is_local)
        except ScandexError as exc:
            self._emit(f"could not read /v2/parties for metadata: {exc}")

    def _remember_party(self, party_id: str, is_local_default: bool = False) -> None:
        """Persist a party, using known metadata when we have it."""
        if not party_id:
            return
        display_name, is_local = self._party_meta.get(
            party_id, (None, is_local_default)
        )
        self.db.save_party(party_id, display_name=display_name, is_local=is_local)

    def _seed(self, stats: IndexStats) -> str:
        """Read ledger-end, then the ACS for every configured party, into the
        ``holdings`` table (and ``ledger_events`` for balance history). Records
        the ledger-end offset so subsequent runs stream forward from there."""
        self._load_party_meta()
        offset = self.ledger.ledger_end()
        self._emit(f"seeding ACS at ledger-end offset={offset}")

        for party in self.parties:
            # A followed party is one we chose to index; default is_local True
            # only if /v2/parties did not tell us otherwise.
            self._remember_party(party, is_local_default=True)
            summary = self.ledger.holdings(party, offset=offset)
            for h in summary.holdings:
                self.db.save_holding(
                    contract_id=h.contract_id,
                    party_id=party,
                    amount=h.amount,
                    instrument=h.instrument,
                    admin=h.administrator,
                    locked=h.locked,
                    ledger_offset=summary.offset,
                )
                # Record the snapshot holding as a "created" event so balance
                # history has a complete starting picture.
                self.db.save_event(
                    ledger_offset=str(summary.offset),
                    event_type="created",
                    contract_id=h.contract_id,
                    template_id=HOLDING_INTERFACE,
                    party_id=party,
                )
                stats.seeded_holdings += 1
            stats.seeded_parties.append(party)
            self._emit(
                f"seeded party={party.split('::')[0]} "
                f"holdings={len(summary.holdings)} "
                f"total={summary.total} spendable={summary.spendable}"
            )

        self.db.save_offset(offset)
        return str(offset)

    # -- phase 2: stream forward -----------------------------------------

    def _catch_up(self, saved_offset: str, stats: IndexStats) -> str:
        """Fetch updates in ``(saved_offset, current_end]`` and apply each one.

        Progress is checkpointed per-update so a crash mid-batch resumes at the
        exact update that failed, never re-reading the ACS.
        """
        current_end = self.ledger.ledger_end()
        if str(current_end) == str(saved_offset):
            self._emit("no new updates since last run")
            return str(current_end)

        begin = _coerce_offset(saved_offset)
        end = _coerce_offset(current_end)
        updates = self.ledger.updates(begin, end, self.parties)
        self._emit(f"fetched {len(updates)} update(s) in ({begin}, {end}]")

        for raw in updates:
            tree = _extract_tree(raw)
            if tree is None:
                continue
            update_id = tree.get("updateId") or tree.get("update_id")
            update_offset = tree.get("offset")
            self._apply_tree(tree, update_id, update_offset, stats)
            stats.updates_processed += 1
            if update_offset is not None:
                self.db.save_offset(str(update_offset))

        # Even if no update carried an offset (unlikely), record the ledger-end
        # we asked up to so we do not re-fetch the same range.
        self.db.save_offset(str(current_end))
        return str(current_end)

    # -- transaction tree walk -------------------------------------------

    def _apply_tree(
        self, tree: dict, update_id: str | None, update_offset,
        stats: IndexStats,
    ) -> None:
        """Walk one transaction tree, applying Holding and TransferInstruction
        created/archived events.

        A tree is *not* a flat list (trap #3 in A1). ``eventsById`` holds both
        created and exercised events keyed by node id. For balance/transfer
        bookkeeping the flat pass over ``eventsById`` is enough - we care about
        which contracts appeared and which disappeared, not the choice nesting.
        """
        events = tree.get("eventsById") or tree.get("events_by_id") or {}
        if isinstance(events, list):
            events = {str(i): e for i, e in enumerate(events)}

        for _event_id, wrapped in events.items():
            kind, event = _unwrap_event(wrapped)
            if event is None:
                continue

            if kind == "created":
                self._apply_created(event, update_id, update_offset, stats)
            elif kind == "exercised":
                self._apply_exercised(event, update_id, update_offset, stats)

    def _apply_created(self, event, update_id, update_offset, stats) -> None:
        holding = _holding_from_created(event)
        if holding is not None:
            owner = holding["owner"] or _first_witness(event)
            if not owner:
                return
            contract_id = event.get("contractId") or holding["contract_id"]
            self._remember_party(owner)
            self.db.save_holding(
                contract_id=contract_id,
                party_id=owner,
                amount=holding["amount"],
                instrument=holding["instrument"],
                admin=holding["administrator"],
                locked=holding["locked"],
                ledger_offset=update_offset,
            )
            self.db.save_event(
                ledger_offset=str(update_offset),
                event_type="created",
                contract_id=contract_id,
                template_id=HOLDING_INTERFACE,
                party_id=owner,
            )
            stats.holdings_created += 1
            # contract_id is part of the per-leg dedupe identity: it is what
            # distinguishes two same-amount credits in one update from a replay
            # of the same one. See store.DEDUPE_INDEX.
            if self.db.save_transfer(
                update_id=update_id, sender=None, receiver=owner,
                amount=holding["amount"], instrument=holding["instrument"],
                transfer_kind="credit", ledger_offset=update_offset,
                status="settled", source="ledger", contract_id=contract_id,
            ):
                stats.transfers_recorded += 1
            return

        # Not a Holding: is it a TransferInstruction offer? (challenge A2)
        offer = _transfer_instruction_from_created(event)
        if offer is not None:
            contract_id = event.get("contractId") or offer["contract_id"]
            for p in (offer["sender"], offer["receiver"]):
                self._remember_party(p)
            self.db.save_event(
                ledger_offset=str(update_offset),
                event_type="created",
                contract_id=contract_id,
                template_id=TRANSFER_INSTRUCTION_INTERFACE,
                party_id=offer["sender"] or offer["receiver"],
            )
            if self.db.save_transfer(
                update_id=update_id, sender=offer["sender"],
                receiver=offer["receiver"], amount=offer["amount"],
                instrument=offer["instrument"], transfer_kind="offer",
                ledger_offset=update_offset, status="pending",
                source="ledger", contract_id=contract_id,
            ):
                stats.offers_created += 1

    def _apply_exercised(self, event, update_id, update_offset, stats) -> None:
        contract_id = event.get("contractId")
        if _is_holding_archive(event):
            if not contract_id:
                return
            row = self.db.conn.execute(
                "SELECT party_id, amount, instrument "
                "FROM holdings WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            self.db.archive_holding(contract_id, update_offset)
            self.db.save_event(
                ledger_offset=str(update_offset),
                event_type="archived",
                contract_id=contract_id,
                template_id=HOLDING_INTERFACE,
                party_id=row["party_id"] if row else None,
            )
            stats.holdings_archived += 1
            if row is None:
                return
            if self.db.save_transfer(
                update_id=update_id, sender=row["party_id"], receiver=None,
                amount=row["amount"], instrument=row["instrument"],
                transfer_kind="debit", ledger_offset=update_offset,
                status="settled", source="ledger", contract_id=contract_id,
            ):
                stats.transfers_recorded += 1
            return

        if _is_transfer_instruction_archive(event):
            if not contract_id:
                return
            # We cannot always tell accept vs reject vs withdraw from the archive
            # event alone; mark the offer "resolved" rather than guess.
            self.db.save_event(
                ledger_offset=str(update_offset),
                event_type="archived",
                contract_id=contract_id,
                template_id=TRANSFER_INSTRUCTION_INTERFACE,
            )
            if self.db.update_transfer_status_by_contract(contract_id, "resolved"):
                stats.offers_resolved += 1

    # -- polling loop (optional; CLI wraps this) --------------------------

    def follow(self, tick_seconds: float = 5.0, max_ticks: int | None = None) -> None:
        """Run ``run_once`` on a loop until interrupted or ``max_ticks``."""
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
    """Ledger API v2 offsets are integers in the wire format; the DB stores them
    as strings so it stays type-agnostic. Coerce back on the way out to the
    ledger."""
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _extract_tree(raw: dict) -> dict | None:
    """The updates response wraps each item in one of a few discriminator shapes
    across Canton versions. Return the inner transaction-tree dict."""
    if not isinstance(raw, dict):
        return None
    for key in ("TransactionTree", "transactionTree", "Transaction", "transaction",
                "update", "Update"):
        inner = raw.get(key)
        if isinstance(inner, dict):
            if "value" in inner and isinstance(inner["value"], dict):
                return inner["value"]
            return inner
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


def _iter_interface_views(created: dict):
    """Yield ``(interface_id, view_value)`` for each interface view on a created
    event, tolerating snake/camel casing."""
    for iv in created.get("interfaceViews", []) or created.get("interface_views", []) or []:
        iface_id = (iv.get("interfaceId") or iv.get("interface_id") or "")
        view = iv.get("viewValue") or iv.get("view_value") or {}
        yield iface_id, view


def _holding_from_created(created: dict) -> dict | None:
    """Extract Holding fields from a created event's interface view, or ``None``
    when the event is not a Holding (some other created contract in the tree)."""
    for iface_id, view in _iter_interface_views(created):
        if HOLDING_INTERFACE not in iface_id and "Holding" not in iface_id:
            continue
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


def _transfer_instruction_from_created(created: dict) -> dict | None:
    """Extract offer fields from a created TransferInstruction interface view.

    Returns ``None`` when the event is not a TransferInstruction. The view shape
    is read defensively: the token standard nests the movement under a
    ``transfer`` object, but we also accept the fields at the top level.

    TODO: verify the exact view shape against a live DevNet response; the field
    names below (transfer.sender / receiver / amount / instrumentId) follow the
    token-standard TransferInstructionView but are NOT confirmed live.
    """
    for iface_id, view in _iter_interface_views(created):
        if (TRANSFER_INSTRUCTION_INTERFACE not in iface_id
                and "TransferInstruction" not in iface_id):
            continue
        transfer = view.get("transfer") or view
        instrument = (transfer.get("instrumentId")
                      or transfer.get("instrument_id") or {})
        return {
            "contract_id": created.get("contractId"),
            "sender": transfer.get("sender"),
            "receiver": transfer.get("receiver"),
            "amount": transfer.get("amount"),
            "instrument": instrument.get("id") if isinstance(instrument, dict) else instrument,
        }
    return None


def _is_holding_archive(exercised: dict) -> bool:
    """True if this exercise consumed a Holding contract."""
    if not exercised.get("consuming", False):
        return False
    iface = exercised.get("interfaceId") or exercised.get("interface_id") or ""
    tmpl = exercised.get("templateId") or exercised.get("template_id") or ""
    return "Holding" in iface or "Holding" in tmpl


def _is_transfer_instruction_archive(exercised: dict) -> bool:
    """True if this exercise consumed a TransferInstruction contract."""
    if not exercised.get("consuming", False):
        return False
    iface = exercised.get("interfaceId") or exercised.get("interface_id") or ""
    tmpl = exercised.get("templateId") or exercised.get("template_id") or ""
    return "TransferInstruction" in iface or "TransferInstruction" in tmpl


def _first_witness(event: dict) -> str | None:
    for key in ("witnessParties", "witness_parties", "signatories", "observers"):
        parties = event.get(key)
        if parties:
            return parties[0]
    return None
