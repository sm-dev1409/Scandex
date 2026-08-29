"""Deterministic demo dataset for ``--data-mode test``.

This is the fabricated data the API serves when it is started with
``--data-mode test``. It exists so the whole stack - database, API, frontend -
can be demonstrated with **no ledger connection, no C8_CLIENT_SECRET, and no
network access at all**. Nothing in this module contacts Cantor8; it only calls
:class:`~scandex_api.store.ScannerDB` write methods.

HONESTY NOTE (read this before showing a demo): every party id, contract id,
amount and transfer below is invented. It is not ledger data and must never be
presented as such. The UI surfaces this as "Data mode: TEST" so nobody mistakes
a seeded number for a real balance.

The shape is chosen to exercise all nine scanner features at once:

* **Alice** holds 50 + 30 unlocked Amulet and 20 *locked* Amulet, so
  ``get_balance`` reports ``total=100, spendable=80, locked_count=1`` - which
  is what makes the "spendable vs locked" distinction (P2) visible rather than
  theoretical. She also holds 2 c8BTC, so per-instrument grouping is exercised
  and no view can get away with summing across instruments.
* **Bob** holds 75 Amulet; **Carol** holds 40 Amulet. Three parties means the
  party selector (P3) has something real to switch between.
* Several settled transfers between them give transfer history (P4) rows in
  both directions.
* One ``pending`` offer (``demo-u4``) is deliberately backdated past the stale
  threshold so stale-transfer detection (P8) has a positive case to find. It is
  backdated with a direct UPDATE because :meth:`ScannerDB.save_transfer` always
  stamps ``recorded_at`` with the current time - there is no legitimate write
  path for "pretend this happened an hour ago", and inventing one on the real
  store class just to serve demo data would be worse than doing it here.
* A saved checkpoint offset means ``/health`` reports ``status="ok"`` rather
  than ``"no_data"``, and the resume path (P6) has a bookmark to resume from.

Seeding is idempotent: :func:`seed_demo_data` clears the tables first, so
restarting the server rebuilds exactly the same dataset instead of stacking a
second copy on top of the first.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .store import DEFAULT_STALE_SECONDS, ScannerDB

# Party ids follow Canton's "<hint>::<fingerprint>" shape so the frontend's
# shortParty() helper has something realistic to truncate. The fingerprints are
# obviously fake on purpose - a real one is a 64-hex-char key hash.
ALICE = "alice-demo::1220alice"
BOB = "bob-demo::1220bob"
CAROL = "carol-demo::1220carol"

# The token admin ("DSO" is the Splice/Amulet operator party).
DSO = "DSO-demo::1220dso"

AMULET = "Amulet"
C8BTC = "c8BTC"

# The offset the seeded checkpoint is left at. Opaque strings are legal ledger
# offsets, but a numeric one lets /health report a real drift number when a
# ledger client is attached.
DEMO_OFFSET = "1000"

# How far past the stale threshold the pending offer is backdated.
STALE_MARGIN_SECONDS = 120


def _clear(db: ScannerDB) -> None:
    """Empty every table without dropping the schema.

    ``ScannerDB.reset()`` would also work, but it drops and recreates the
    tables (and the WAL/index setup with them); a DELETE keeps the open
    connection's schema intact and is enough to make re-seeding idempotent.
    """
    for table in ("transfers", "holdings", "ledger_events", "parties", "checkpoint"):
        db.conn.execute(f"DELETE FROM {table}")
    db.conn.commit()


def seed_demo_data(db: ScannerDB, stale_seconds: int | None = None) -> dict:
    """Populate ``db`` with the deterministic Alice/Bob/Carol dataset.

    Args:
        db: an open :class:`ScannerDB`. Its tables are cleared first.
        stale_seconds: the staleness threshold the pending offer should be
            backdated past. Defaults to the database's own ``stale_seconds``
            so a server started with ``--stale-seconds`` still sees the demo
            offer as stale.

    Returns:
        A small summary dict (counts and the party ids) so callers can log what
        was seeded without re-querying.
    """
    threshold = stale_seconds if stale_seconds is not None else getattr(
        db, "stale_seconds", DEFAULT_STALE_SECONDS)

    _clear(db)

    # -- parties (P3) -----------------------------------------------------
    # Alice is the "local" party: the one this scanner would be running as, so
    # the frontend's selector defaults to her.
    db.save_party(ALICE, display_name="Alice", is_local=True)
    db.save_party(BOB, display_name="Bob", is_local=False)
    db.save_party(CAROL, display_name="Carol", is_local=False)

    # -- holdings (P1/P2) -------------------------------------------------
    # Alice: 50 + 30 spendable, 20 locked => total 100, spendable 80.
    db.save_holding("demo-h-a50", ALICE, "50", AMULET, DSO, False, DEMO_OFFSET)
    db.save_holding("demo-h-a30", ALICE, "30", AMULET, DSO, False, DEMO_OFFSET)
    db.save_holding("demo-h-a20L", ALICE, "20", AMULET, DSO, True, DEMO_OFFSET)
    # A second instrument, so nothing can collapse balances into one number.
    db.save_holding("demo-h-abtc", ALICE, "2", C8BTC, DSO, False, DEMO_OFFSET)
    db.save_holding("demo-h-b75", BOB, "75", AMULET, DSO, False, DEMO_OFFSET)
    db.save_holding("demo-h-c40", CAROL, "40", AMULET, DSO, False, DEMO_OFFSET)

    # An archived holding: proves balances exclude spent notes while the audit
    # trail keeps the row (active=0).
    db.save_holding("demo-h-a15-spent", ALICE, "15", AMULET, DSO, False, "990")
    db.archive_holding("demo-h-a15-spent", DEMO_OFFSET)

    # -- raw events (the audit trail) -------------------------------------
    db.save_event(DEMO_OFFSET, "created", "demo-h-a50",
                  template_id="Splice.Amulet:Amulet", party_id=ALICE)
    db.save_event(DEMO_OFFSET, "created", "demo-h-b75",
                  template_id="Splice.Amulet:Amulet", party_id=BOB)
    db.save_event(DEMO_OFFSET, "archived", "demo-h-a15-spent",
                  template_id="Splice.Amulet:Amulet", party_id=ALICE)

    # -- transfers (P4) ---------------------------------------------------
    # Settled history, in both directions so the dashboard's sent/received
    # split and the direction filter both have rows to work with.
    db.save_transfer("demo-u1", ALICE, BOB, "25", AMULET, "direct",
                     ledger_offset="995", contract_id="demo-tx-1")
    db.save_transfer("demo-u2", BOB, ALICE, "10", AMULET, "direct",
                     ledger_offset="997", contract_id="demo-tx-2")
    db.save_transfer("demo-u3", ALICE, CAROL, "5", AMULET, "direct",
                     ledger_offset="999", contract_id="demo-tx-3")
    db.save_transfer("demo-u5", CAROL, ALICE, "1", C8BTC, "direct",
                     ledger_offset=DEMO_OFFSET, contract_id="demo-tx-5")

    # -- the stale pending offer (P8) -------------------------------------
    # save_transfer() stamps recorded_at = now, so write it then backdate it.
    db.save_transfer("demo-u4", ALICE, BOB, "5", AMULET, "offer",
                     ledger_offset="998", status="pending",
                     contract_id="demo-ti-1")
    stale_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=threshold + STALE_MARGIN_SECONDS)
    ).isoformat()
    db.conn.execute(
        "UPDATE transfers SET recorded_at = ? WHERE update_id = 'demo-u4'",
        (stale_at,),
    )
    db.conn.commit()

    # -- checkpoint (P5/P6) ----------------------------------------------
    # A saved offset is what makes /health report "ok" instead of "no_data".
    db.save_offset(DEMO_OFFSET)

    return {
        "parties": [ALICE, BOB, CAROL],
        "holdings": 7,
        "transfers": 5,
        "stale_pending": 1,
        "offset": DEMO_OFFSET,
    }
