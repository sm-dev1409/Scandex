"""Offline tests for the A1 indexer, writing through scandex_api.store.ScannerDB.

Ported from the db.py/Database era: the tree-walking fixtures are unchanged
(that logic was already correct), only the persistence assertions moved to the
ScannerDB table and column names.

No test here touches the network - everything is answered by FakeTransport.
"""
import tempfile
import unittest
from pathlib import Path

from _fakes import FakeTransport, make_config, token_response

from scandex_api.auth import Authenticator
from scandex_api.http import HttpClient
from scandex_api.indexer import Indexer
from scandex_api.ledger import (
    HOLDING_INTERFACE,
    TRANSFER_INSTRUCTION_INTERFACE,
    LedgerClient,
)
from scandex_api.store import ScannerDB


def _wire(transport, party="alice::1"):
    cfg = make_config(party=party)
    http = HttpClient(timeout=5.0, retries=0, transport=transport)
    auth = Authenticator(cfg, http)
    ledger = LedgerClient(cfg, auth, http)
    return cfg, ledger


def _base_transport():
    return FakeTransport().add("POST", "/openid-connect/token",
                               json_body=token_response())


def _holding_entry(amount, owner="alice::1", instrument="Amulet",
                   admin="DSO::1", locked=False, cid="cid-1"):
    lock = {"expiresAt": "2030-01-01T00:00:00Z"} if locked else None
    return {"contractEntry": {"JsActiveContract": {"createdEvent": {
        "contractId": cid,
        "interfaceViews": [{
            "interfaceId": HOLDING_INTERFACE,
            "viewValue": {
                "owner": owner,
                "amount": amount,
                "instrumentId": {"id": instrument, "admin": admin},
                "lock": lock,
            },
        }],
    }}}}


def _tree_update(update_id, offset, events):
    return {"TransactionTree": {
        "updateId": update_id,
        "offset": offset,
        "eventsById": events,
    }}


def _created_holding_event(cid, owner, amount, instrument="Amulet", admin="DSO::1"):
    return {"CreatedTreeEvent": {"value": {
        "contractId": cid,
        "witnessParties": [owner],
        "interfaceViews": [{
            "interfaceId": HOLDING_INTERFACE,
            "viewValue": {
                "owner": owner,
                "amount": amount,
                "instrumentId": {"id": instrument, "admin": admin},
                "lock": None,
            },
        }],
    }}}


def _archive_holding_event(cid):
    return {"ExercisedTreeEvent": {"value": {
        "contractId": cid,
        "consuming": True,
        "interfaceId": HOLDING_INTERFACE,
        "choice": "Archive",
        "actingParties": [],
        "witnessParties": [],
    }}}


def _created_offer_event(cid, sender, receiver, amount,
                         instrument="Amulet", admin="DSO::1"):
    """A TransferInstruction created event (a pending offer).

    NOTE: the interface id and view shape here follow the token standard's
    documented naming but have NOT been confirmed against a live DevNet
    response - see the TODO on TRANSFER_INSTRUCTION_INTERFACE in ledger.py.
    This fixture therefore tests our handling of that shape, not that the
    shape is the one DevNet really sends.
    """
    return {"CreatedTreeEvent": {"value": {
        "contractId": cid,
        "witnessParties": [sender],
        "interfaceViews": [{
            "interfaceId": TRANSFER_INSTRUCTION_INTERFACE,
            "viewValue": {
                "transfer": {
                    "sender": sender,
                    "receiver": receiver,
                    "amount": amount,
                    "instrumentId": {"id": instrument, "admin": admin},
                },
            },
        }],
    }}}


def _archive_offer_event(cid):
    return {"ExercisedTreeEvent": {"value": {
        "contractId": cid,
        "consuming": True,
        "interfaceId": TRANSFER_INSTRUCTION_INTERFACE,
        "choice": "TransferInstruction_Accept",
        "actingParties": [],
        "witnessParties": [],
    }}}


class IndexerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scandex.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _indexer(self, transport, party="alice::1"):
        cfg, ledger = _wire(transport, party=party)
        db = ScannerDB(self.db_path)
        return db, Indexer(db, ledger, [party])


class SeedTests(IndexerTestCase):
    def test_first_run_seeds_from_acs_and_saves_offset(self):
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
             .add("POST", "/v2/state/active-contracts", json_body=[
                 _holding_entry("100.0", cid="cid-1"),
                 _holding_entry("40.0", cid="cid-2", locked=True),
             ]))
        db, idx = self._indexer(t)
        try:
            stats = idx.run_once()
        finally:
            db.close()
        self.assertEqual(stats.seeded_holdings, 2)
        self.assertEqual(stats.updates_processed, 0)

        db2 = ScannerDB(self.db_path)
        try:
            self.assertEqual(db2.get_offset(), "100")
            rows = db2.get_balance("alice::1")
            self.assertAlmostEqual(rows[0]["total"], 140.0)
            self.assertAlmostEqual(rows[0]["spendable"], 100.0)
            # Seeded holdings are also logged as events so balance history has
            # a complete starting picture.
            self.assertEqual(db2.conn.execute(
                "SELECT COUNT(*) AS c FROM ledger_events").fetchone()["c"], 2)
        finally:
            db2.close()
        self.assertEqual(t.call_count("POST", "/v2/state/active-contracts"), 1)

    def test_seed_records_followed_party_with_ledger_metadata(self):
        """P3: nothing used to fill the parties table from the live pipeline.
        The seed now reads /v2/parties for accurate display_name / is_local."""
        t = (_base_transport()
             .add("GET", "/v2/parties", json_body={"partyDetails": [
                 {"party": "alice::1", "isLocal": True,
                  "localMetadata": {"annotations": {"displayName": "Alice"}}},
             ]})
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
             .add("POST", "/v2/state/active-contracts",
                  json_body=[_holding_entry("100.0", cid="cid-1")]))
        db, idx = self._indexer(t)
        try:
            idx.run_once()
            parties = db.get_parties()
        finally:
            db.close()
        self.assertEqual(len(parties), 1)
        self.assertEqual(parties[0]["party_id"], "alice::1")
        self.assertEqual(parties[0]["display_name"], "Alice")
        self.assertEqual(parties[0]["is_local"], 1)

    def test_seed_survives_an_unavailable_parties_endpoint(self):
        """/v2/parties is best-effort metadata: losing it must not stop the
        seed, just fall back to defaults."""
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
             .add("POST", "/v2/state/active-contracts",
                  json_body=[_holding_entry("100.0", cid="cid-1")]))
        # No /v2/parties route registered -> FakeTransport answers 404.
        db, idx = self._indexer(t)
        try:
            stats = idx.run_once()
            parties = db.get_parties()
        finally:
            db.close()
        self.assertEqual(stats.seeded_holdings, 1)
        self.assertEqual(len(parties), 1)
        self.assertIsNone(parties[0]["display_name"])


class RestartTests(IndexerTestCase):
    def test_restart_resumes_from_saved_offset_without_reseeding(self):
        """P6, the whole point of the checkpoint table: a fresh Indexer and a
        fresh ScannerDB over the same file must take the catch-up path."""
        # -- process 1: seed --
        t1 = (_base_transport()
              .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
              .add("POST", "/v2/state/active-contracts",
                   json_body=[_holding_entry("100.0", cid="cid-1")]))
        db1, idx1 = self._indexer(t1)
        try:
            idx1.run_once()
            self.assertEqual(db1.get_offset(), "100")
        finally:
            db1.close()
        self.assertEqual(t1.call_count("POST", "/v2/state/active-contracts"), 1)

        # -- process 2: a brand new Indexer/ScannerDB over the same file --
        t2 = (_base_transport()
              .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
              .add("POST", "/v2/updates/trees", json_body={"updates": []}))
        db2, idx2 = self._indexer(t2)
        try:
            stats = idx2.run_once()
            # The restart guarantee: no second ACS read, ever.
            self.assertEqual(t2.call_count("POST", "/v2/state/active-contracts"), 0)
            self.assertEqual(stats.start_offset, "100")
            self.assertEqual(stats.seeded_holdings, 0)
            self.assertEqual(stats.seeded_parties, [])
            # And the seeded state is still there.
            self.assertAlmostEqual(db2.get_balance("alice::1")[0]["total"], 100.0)
        finally:
            db2.close()

    def test_resume_with_no_new_updates_does_not_fetch_trees(self):
        pre = ScannerDB(self.db_path)
        try:
            pre.save_offset("100")
        finally:
            pre.close()
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
             .add("POST", "/v2/updates/trees", json_body={"updates": []}))
        db, idx = self._indexer(t)
        try:
            stats = idx.run_once()
        finally:
            db.close()
        self.assertEqual(t.call_count("POST", "/v2/updates/trees"), 0)
        self.assertEqual(stats.updates_processed, 0)


class TreeWalkTests(IndexerTestCase):
    def _preseed(self):
        pre = ScannerDB(self.db_path)
        try:
            pre.save_offset("100")
            pre.save_holding("cid-1", "alice::1", "100.0", "Amulet", "DSO::1",
                             False, "100")
        finally:
            pre.close()

    def test_updates_walk_records_created_and_archived_holdings(self):
        self._preseed()
        # Transfer of 30 from alice -> bob at offset 101:
        #   archive cid-1 (alice, 100), create cid-2 (bob, 30) receive side,
        #   create cid-3 (alice, 70) change back to alice.
        tree = _tree_update("upd-1", 101, {
            "0": _archive_holding_event("cid-1"),
            "1": _created_holding_event("cid-2", "bob::1", "30.0"),
            "2": _created_holding_event("cid-3", "alice::1", "70.0"),
        })
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 101})
             .add("POST", "/v2/updates/trees", json_body={"updates": [tree]}))
        db, idx = self._indexer(t)
        try:
            stats = idx.run_once()
            self.assertEqual(t.call_count("POST", "/v2/state/active-contracts"), 0)
            self.assertEqual(stats.updates_processed, 1)
            self.assertEqual(stats.holdings_created, 2)
            self.assertEqual(stats.holdings_archived, 1)

            self.assertAlmostEqual(db.get_balance("alice::1")[0]["total"], 70.0)
            self.assertAlmostEqual(db.get_balance("bob::1")[0]["total"], 30.0)

            # Per-leg transfer rows: a debit for alice (the archive) and a
            # credit each for bob and alice's change.
            kinds = sorted(r["transfer_kind"] for r in db.get_transfers("alice::1"))
            self.assertIn("debit", kinds)
            self.assertIn("credit", kinds)

            bob_transfers = db.get_transfers("bob::1")
            self.assertEqual(len(bob_transfers), 1)
            self.assertEqual(bob_transfers[0]["transfer_kind"], "credit")
            self.assertEqual(bob_transfers[0]["amount"], "30.0")
            self.assertEqual(bob_transfers[0]["update_id"], "upd-1")
            self.assertEqual(bob_transfers[0]["status"], "settled")
            self.assertEqual(bob_transfers[0]["source"], "ledger")

            self.assertEqual(db.get_offset(), "101")
        finally:
            db.close()

    def test_a_counterparty_is_registered_as_a_party(self):
        """P3: a transfer counterparty is not one of the parties we follow, but
        it must still show up in /parties."""
        self._preseed()
        tree = _tree_update("upd-1", 101, {
            "0": _created_holding_event("cid-2", "bob::1", "30.0"),
        })
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 101})
             .add("POST", "/v2/updates/trees", json_body={"updates": [tree]}))
        db, idx = self._indexer(t)
        try:
            idx.run_once()
            ids = [p["party_id"] for p in db.get_parties()]
        finally:
            db.close()
        self.assertIn("bob::1", ids)

    def test_replaying_the_same_update_does_not_double_count(self):
        """Idempotence end to end: the same tree applied twice yields the same
        balances and one transfer row per leg, not two."""
        self._preseed()
        tree = _tree_update("upd-1", 101, {
            "0": _created_holding_event("cid-2", "bob::1", "30.0"),
        })
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 101})
             .add("POST", "/v2/updates/trees", json_body={"updates": [tree]}))
        db, idx = self._indexer(t)
        try:
            idx.run_once()
            # Rewind the checkpoint as a crash-before-save would have left it.
            db.save_offset("100")
            idx.run_once()
            self.assertAlmostEqual(db.get_balance("bob::1")[0]["total"], 30.0)
            self.assertEqual(len(db.get_transfers("bob::1")), 1)
        finally:
            db.close()

    def test_two_same_amount_credits_in_one_update_are_both_kept(self):
        """The dedupe identity includes the holding contract id, so one update
        legitimately crediting a party twice for the same amount is two rows -
        not one row plus a false 'already seen this' drop."""
        self._preseed()
        tree = _tree_update("upd-1", 101, {
            "0": _created_holding_event("cid-a", "bob::1", "25.0"),
            "1": _created_holding_event("cid-b", "bob::1", "25.0"),
        })
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 101})
             .add("POST", "/v2/updates/trees", json_body={"updates": [tree]}))
        db, idx = self._indexer(t)
        try:
            idx.run_once()
            rows = db.get_transfers("bob::1")
            self.assertEqual(len(rows), 2)
            self.assertCountEqual([r["contract_id"] for r in rows],
                                  ["cid-a", "cid-b"])
            self.assertAlmostEqual(db.get_balance("bob::1")[0]["total"], 50.0)
        finally:
            db.close()

    def test_updates_request_is_bounded_and_asks_for_both_interfaces(self):
        pre = ScannerDB(self.db_path)
        try:
            pre.save_offset("100")
        finally:
            pre.close()
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 105})
             .add("POST", "/v2/updates/trees", json_body={"updates": []}))
        db, idx = self._indexer(t)
        try:
            idx.run_once()
        finally:
            db.close()
        bodies = t.bodies_sent("/v2/updates/trees")
        self.assertEqual(len(bodies), 1)
        self.assertEqual(bodies[0]["beginExclusive"], 100)
        self.assertEqual(bodies[0]["endInclusive"], 105)
        cumulative = bodies[0]["filter"]["filtersByParty"]["alice::1"]["cumulative"]
        # P8 needed this broadened: Holding alone never surfaces offers.
        ids = [c["identifierFilter"]["InterfaceFilter"]["value"]["interfaceId"]
               for c in cumulative]
        self.assertIn(HOLDING_INTERFACE, ids)
        self.assertIn(TRANSFER_INSTRUCTION_INTERFACE, ids)


class TransferInstructionTests(IndexerTestCase):
    """P8: offers land as pending transfers and age into the stale list."""

    def _run_with(self, events, offset=101, db=None):
        pre = db or ScannerDB(self.db_path)
        close_pre = db is None
        try:
            pre.save_offset(str(offset - 1))
        finally:
            if close_pre:
                pre.close()
        tree = _tree_update("upd-offer", offset, events)
        t = (_base_transport()
             .add("GET", "/v2/state/ledger-end", json_body={"offset": offset})
             .add("POST", "/v2/updates/trees", json_body={"updates": [tree]}))
        return self._indexer(t)

    def test_created_transfer_instruction_is_recorded_as_pending(self):
        db, idx = self._run_with({
            "0": _created_offer_event("ti-1", "alice::1", "bob::1", "25.0"),
        })
        try:
            stats = idx.run_once()
            self.assertEqual(stats.offers_created, 1)
            rows = db.get_transfers("alice::1")
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["transfer_kind"], "offer")
            self.assertEqual(row["status"], "pending")
            self.assertEqual(row["contract_id"], "ti-1")
            self.assertEqual(row["sender"], "alice::1")
            self.assertEqual(row["receiver"], "bob::1")
            self.assertEqual(row["amount"], "25.0")
            self.assertEqual(row["instrument"], "Amulet")
            # Both sides of the offer are registered as parties.
            self.assertCountEqual([p["party_id"] for p in db.get_parties()],
                                  ["alice::1", "bob::1"])
        finally:
            db.close()

    def test_a_pending_offer_is_stale_only_once_past_the_threshold(self):
        db, idx = self._run_with({
            "0": _created_offer_event("ti-1", "alice::1", "bob::1", "25.0"),
        })
        try:
            idx.run_once()
            # Just recorded: not stale under the default 300s threshold.
            self.assertEqual(db.get_stale_transfers(), [])
            # With a zero-second threshold, the same row is stale.
            stale = db.get_stale_transfers(0)
            self.assertEqual(len(stale), 1)
            self.assertEqual(stale[0]["contract_id"], "ti-1")
            self.assertEqual(db.get_health()["stale_pending_transfers"], 0)
        finally:
            db.close()

    def test_archived_transfer_instruction_resolves_the_pending_row(self):
        """We cannot tell accept from reject from withdraw out of the archive
        event alone, so the status becomes 'resolved' rather than a guess."""
        db, idx = self._run_with({
            "0": _created_offer_event("ti-1", "alice::1", "bob::1", "25.0"),
            "1": _archive_offer_event("ti-1"),
        })
        try:
            stats = idx.run_once()
            self.assertEqual(stats.offers_created, 1)
            self.assertEqual(stats.offers_resolved, 1)
            row = db.get_transfers("alice::1")[0]
            self.assertEqual(row["status"], "resolved")
            # Resolved offers drop out of the stale list at any threshold.
            self.assertEqual(db.get_stale_transfers(0), [])
        finally:
            db.close()

    def test_an_offer_is_not_mistaken_for_a_holding(self):
        db, idx = self._run_with({
            "0": _created_offer_event("ti-1", "alice::1", "bob::1", "25.0"),
        })
        try:
            stats = idx.run_once()
            self.assertEqual(stats.holdings_created, 0)
            self.assertEqual(db.get_holdings_raw("alice::1"), [])
            self.assertEqual(db.get_balance("alice::1"), [])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
