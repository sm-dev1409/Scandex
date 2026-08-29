import tempfile
import unittest
from pathlib import Path

from _fakes import FakeTransport, make_config, token_response

from scandex_api.auth import Authenticator
from scandex_api.db import UPDATES_STREAM, Database
from scandex_api.http import HttpClient
from scandex_api.indexer import Indexer
from scandex_api.ledger import HOLDING_INTERFACE, LedgerClient


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


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scandex.db"

    def tearDown(self):
        self.tmp.cleanup()

    def _indexer(self, transport, party="alice::1"):
        cfg, ledger = _wire(transport, party=party)
        db = Database(self.db_path)
        return db, Indexer(db, ledger, [party])

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
        # Offset is persisted so a restart won't re-seed.
        db2 = Database(self.db_path)
        try:
            self.assertEqual(db2.get_offset(), "100")
            rows = db2.balance_for("alice::1")
            self.assertAlmostEqual(rows[0]["total"], 140.0)
            self.assertAlmostEqual(rows[0]["spendable"], 100.0)
        finally:
            db2.close()
        # The A1 restart guarantee: the seed pass called ACS, and the offset was saved.
        self.assertEqual(t.call_count("POST", "/v2/state/active-contracts"), 1)

    def test_restart_resumes_from_saved_offset_without_reseeding(self):
        # Pre-populate the DB as though a previous run had already seeded.
        pre = Database(self.db_path)
        try:
            pre.set_offset("100", stream=UPDATES_STREAM)
            pre.upsert_holding(
                contract_id="cid-1", party_id="alice::1", amount="100.0",
                instrument_id="Amulet", administrator="DSO::1", locked=False,
                lock_expiry=None, read_at_offset="100",
            )
        finally:
            pre.close()

        t = (_base_transport()
             # Two ledger-end calls: the resume path checks it, then the batch is empty.
             .add("GET", "/v2/state/ledger-end", json_body={"offset": 100})
             .add("POST", "/v2/updates/trees", json_body={"updates": []}))
        db, idx = self._indexer(t)
        try:
            stats = idx.run_once()
        finally:
            db.close()
        # Critically: the indexer did NOT re-query the ACS on restart.
        self.assertEqual(t.call_count("POST", "/v2/state/active-contracts"), 0)
        self.assertEqual(stats.start_offset, "100")
        # And balance is still there.
        db2 = Database(self.db_path)
        try:
            rows = db2.balance_for("alice::1")
            self.assertAlmostEqual(rows[0]["total"], 100.0)
        finally:
            db2.close()

    def test_updates_walk_records_created_and_archived_holdings(self):
        # Seed: alice starts with 100.0 in cid-1.
        pre = Database(self.db_path)
        try:
            pre.set_offset("100", stream=UPDATES_STREAM)
            pre.upsert_holding(
                contract_id="cid-1", party_id="alice::1", amount="100.0",
                instrument_id="Amulet", administrator="DSO::1", locked=False,
                lock_expiry=None, read_at_offset="100",
            )
        finally:
            pre.close()

        # Transfer of 30 from alice -> bob at offset 101:
        # - archive cid-1 (alice, 100)
        # - create cid-2 (bob,  30)   receive side
        # - create cid-3 (alice, 70)  change back to alice
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
            # ACS still not touched on a resume.
            self.assertEqual(t.call_count("POST", "/v2/state/active-contracts"), 0)
            self.assertEqual(stats.updates_processed, 1)
            self.assertEqual(stats.holdings_created, 2)
            self.assertEqual(stats.holdings_archived, 1)

            rows = db.balance_for("alice::1")
            # cid-1 archived, cid-3 (70.0) remains for alice
            self.assertAlmostEqual(rows[0]["total"], 70.0)
            bob_rows = db.balance_for("bob::1")
            self.assertAlmostEqual(bob_rows[0]["total"], 30.0)

            # Transfer rows: one debit for alice (the archive), one credit each
            # for bob and alice-change.
            transfers = db.transfers_for("alice::1")
            kinds = sorted(r["transfer_kind"] for r in transfers)
            self.assertIn("debit", kinds)
            self.assertIn("credit", kinds)
            bob_transfers = db.transfers_for("bob::1")
            self.assertEqual(len(bob_transfers), 1)
            self.assertEqual(bob_transfers[0]["transfer_kind"], "credit")
            self.assertEqual(bob_transfers[0]["amount"], "30.0")
            self.assertEqual(bob_transfers[0]["update_id"], "upd-1")

            # Offset was advanced to the ledger-end we asked up to.
            self.assertEqual(db.get_offset(), "101")
        finally:
            db.close()

    def test_updates_request_is_bounded_and_uses_interface_filter(self):
        pre = Database(self.db_path)
        try:
            pre.set_offset("100", stream=UPDATES_STREAM)
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
        self.assertIn("InterfaceFilter", cumulative[0]["identifierFilter"])


if __name__ == "__main__":
    unittest.main()
