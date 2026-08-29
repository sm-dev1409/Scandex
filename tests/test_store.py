"""Offline tests for the canonical database layer, scandex_api.store.ScannerDB.

Replaces the old tests/test_db.py (the Database class it covered is gone).

The fixture numbers come from store.py's own self_test() walkthrough, turned
into real assertions instead of printed output:

    seed        Alice: 50 + 30 + 20(locked)  -> total=100, spendable=80
                Bob:   75                    -> total=75
    transfer    Alice sends Bob 25 Amulet: archive Alice's 50, create 25 change
                for Alice and 25 for Bob
    after       Alice: total=75, spendable=55
                Bob:   total=100
"""
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _fakes import make_config  # noqa: F401 - ensures src/ is on sys.path

from scandex_api.store import DEFAULT_STALE_SECONDS, SCHEMA_VERSION, ScannerDB

ALICE = "alice::1"
BOB = "bob::1"
DSO = "DSO::1"


class StoreTestCase(unittest.TestCase):
    """Shared temp-file database. A file (not :memory:) so the reopen/restart
    tests exercise the real path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scandex.db"
        self.db = ScannerDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def _seed(self):
        """The self_test() starting picture."""
        self.db.save_party(ALICE, display_name="Alice", is_local=True)
        self.db.save_party(BOB, display_name="Bob", is_local=False)
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "10")
        self.db.save_holding("a-30", ALICE, "30", "Amulet", DSO, False, "10")
        self.db.save_holding("a-20", ALICE, "20", "Amulet", DSO, True, "10")
        self.db.save_holding("b-75", BOB, "75", "Amulet", DSO, False, "10")
        for cid, party in (("a-50", ALICE), ("a-30", ALICE),
                           ("a-20", ALICE), ("b-75", BOB)):
            self.db.save_event("10", "created", cid, party_id=party)
        self.db.save_offset("10")


class SchemaTests(StoreTestCase):
    def test_schema_is_idempotent_and_versioned(self):
        self.db.save_offset("77")
        self.db.close()
        again = ScannerDB(self.db_path)
        try:
            self.assertEqual(again.get_offset(), "77")
            version = again.conn.execute(
                "SELECT version FROM schema_version").fetchone()["version"]
            self.assertEqual(version, SCHEMA_VERSION)
            # Re-opening must not have inserted a second version row.
            self.assertEqual(
                again.conn.execute(
                    "SELECT COUNT(*) AS c FROM schema_version").fetchone()["c"], 1)
        finally:
            again.close()

    def test_every_expected_table_exists(self):
        names = {r["name"] for r in self.db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertLessEqual(
            {"checkpoint", "parties", "holdings", "ledger_events",
             "transfers", "schema_version"}, names)

    def test_reset_clears_everything(self):
        self._seed()
        self.assertTrue(self.db.stats()["holdings_active"])
        self.db.reset()
        self.assertEqual(self.db.stats()["holdings_active"], 0)
        self.assertIsNone(self.db.get_offset())


class OffsetTests(StoreTestCase):
    def test_offset_round_trip(self):
        self.assertIsNone(self.db.get_offset())
        self.db.save_offset(42)
        self.assertEqual(self.db.get_offset(), "42")
        self.db.save_offset("43")
        self.assertEqual(self.db.get_offset(), "43")
        # One bookmark, not a growing log.
        self.assertEqual(self.db.conn.execute(
            "SELECT COUNT(*) AS c FROM checkpoint").fetchone()["c"], 1)


class PartyTests(StoreTestCase):
    def test_save_party_inserts_then_refreshes(self):
        self.db.save_party(ALICE, display_name="Alice", is_local=True)
        rows = self.db.get_parties()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["party_id"], ALICE)
        self.assertEqual(rows[0]["display_name"], "Alice")
        self.assertEqual(rows[0]["is_local"], 1)

        first_seen = rows[0]["first_seen_at"]
        # A later sighting with no name must not clobber the known one.
        self.db.save_party(ALICE, display_name=None, is_local=True)
        rows = self.db.get_parties()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["display_name"], "Alice")
        self.assertEqual(rows[0]["first_seen_at"], first_seen)

    def test_get_parties_lists_all(self):
        self._seed()
        ids = [p["party_id"] for p in self.db.get_parties()]
        self.assertCountEqual(ids, [ALICE, BOB])


class BalanceTests(StoreTestCase):
    def test_seed_balance_total_and_spendable(self):
        """P1/P2: 50 + 30 + 20-locked -> total=100, spendable=80."""
        self._seed()
        rows = self.db.get_balance(ALICE)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["instrument"], "Amulet")
        self.assertAlmostEqual(row["total"], 100.0)
        self.assertAlmostEqual(row["spendable"], 80.0)
        self.assertEqual(row["holding_count"], 3)
        self.assertEqual(row["locked_count"], 1)

    def test_balance_after_transfer_matches_self_test_numbers(self):
        self._seed()
        # Alice sends Bob 25: her 50 is consumed, 25 comes back as change.
        self.db.archive_holding("a-50", "11")
        self.db.save_holding("a-25", ALICE, "25", "Amulet", DSO, False, "11")
        self.db.save_holding("b-25", BOB, "25", "Amulet", DSO, False, "11")

        alice = self.db.get_balance(ALICE)[0]
        self.assertAlmostEqual(alice["total"], 75.0)
        self.assertAlmostEqual(alice["spendable"], 55.0)

        bob = self.db.get_balance(BOB)[0]
        self.assertAlmostEqual(bob["total"], 100.0)
        self.assertAlmostEqual(bob["spendable"], 100.0)

    def test_balance_filtered_by_instrument(self):
        self._seed()
        self.db.save_holding("a-btc", ALICE, "2", "c8BTC", DSO, False, "10")
        self.assertEqual(len(self.db.get_balance(ALICE)), 2)
        only = self.db.get_balance(ALICE, instrument="c8BTC")
        self.assertEqual(len(only), 1)
        self.assertAlmostEqual(only[0]["total"], 2.0)

    def test_unknown_party_balance_is_empty_not_an_error(self):
        self._seed()
        self.assertEqual(self.db.get_balance("nobody::9"), [])

    def test_archived_holdings_are_kept_but_excluded(self):
        self._seed()
        self.db.archive_holding("a-50", "11")
        self.assertAlmostEqual(self.db.get_balance(ALICE)[0]["total"], 50.0)
        # The row survives for history.
        all_rows = self.db.get_holdings_raw(ALICE, active_only=False)
        archived = [r for r in all_rows if r["contract_id"] == "a-50"]
        self.assertEqual(len(archived), 1)
        self.assertEqual(archived[0]["active"], 0)
        self.assertEqual(archived[0]["archived_at_offset"], "11")

    def test_save_holding_is_idempotent_and_revives_a_recreated_contract(self):
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "10")
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "10")
        self.assertEqual(len(self.db.get_holdings_raw(ALICE)), 1)
        self.db.archive_holding("a-50", "11")
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "12")
        row = self.db.get_holdings_raw(ALICE)[0]
        self.assertEqual(row["active"], 1)
        self.assertIsNone(row["archived_at_offset"])


class HoldingsRawTests(StoreTestCase):
    def test_holdings_raw_exposes_the_locked_flag_per_banknote(self):
        """P2 detail view: the individual holdings, not a sum."""
        self._seed()
        rows = self.db.get_holdings_raw(ALICE)
        self.assertEqual(len(rows), 3)
        by_cid = {r["contract_id"]: r for r in rows}
        self.assertEqual(by_cid["a-20"]["locked"], 1)
        self.assertEqual(by_cid["a-50"]["locked"], 0)
        self.assertEqual(by_cid["a-50"]["admin"], DSO)


class OwnersTests(StoreTestCase):
    def test_get_owners_groups_by_party_and_instrument(self):
        self._seed()
        owners = self.db.get_owners()
        self.assertEqual(len(owners), 2)
        totals = {o["party_id"]: o["total"] for o in owners}
        self.assertAlmostEqual(totals[ALICE], 100.0)
        self.assertAlmostEqual(totals[BOB], 75.0)
        # Sorted by total descending.
        self.assertEqual(owners[0]["party_id"], ALICE)

    def test_get_owners_filtered_by_instrument(self):
        self._seed()
        self.db.save_holding("a-btc", ALICE, "2", "c8BTC", DSO, False, "10")
        btc = self.db.get_owners(instrument="c8BTC")
        self.assertEqual(len(btc), 1)
        self.assertEqual(btc[0]["party_id"], ALICE)


class TransferTests(StoreTestCase):
    def test_save_transfer_is_idempotent(self):
        """The restart guarantee: replaying an offset range must not
        double-insert. UNIQUE(update_id, sender, receiver, instrument, amount)."""
        first = self.db.save_transfer("u1", ALICE, BOB, "5.0", "Amulet", "direct")
        second = self.db.save_transfer("u1", ALICE, BOB, "5.0", "Amulet", "direct")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(len(self.db.get_transfers(ALICE)), 1)

        # A genuinely different transfer still inserts.
        self.assertTrue(self.db.save_transfer("u1", ALICE, BOB, "6.0", "Amulet",
                                              "direct"))
        self.assertEqual(len(self.db.get_transfers(ALICE)), 2)

    def test_the_unique_constraint_actually_exists(self):
        """Belt and braces: assert the constraint, not just save_transfer's
        return value, so removing it can never pass silently."""
        self.db.conn.execute(
            "INSERT INTO transfers (update_id, sender, receiver, amount, "
            "instrument, recorded_at) VALUES ('u9', 'a', 'b', '1', 'Amulet', 'now')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute(
                "INSERT INTO transfers (update_id, sender, receiver, amount, "
                "instrument, recorded_at) VALUES ('u9', 'a', 'b', '1', 'Amulet', 'now')")
        self.db.conn.rollback()

    def test_transfers_are_returned_for_sender_and_receiver(self):
        self.db.save_transfer("u1", ALICE, BOB, "25", "Amulet", "direct")
        self.assertEqual(len(self.db.get_transfers(ALICE)), 1)
        self.assertEqual(len(self.db.get_transfers(BOB)), 1)
        self.assertEqual(self.db.get_transfers("carol::1"), [])

    def test_transfers_newest_first_and_limited(self):
        for i in range(5):
            self.db.save_transfer(f"u{i}", ALICE, BOB, str(i), "Amulet", "direct")
        rows = self.db.get_transfers(ALICE, limit=3)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["amount"] for r in rows], ["4", "3", "2"])

    def test_transfer_detail_returns_every_leg_of_one_update(self):
        """P4: the indexer records per-leg credit/debit rows, so one update id
        maps to several rows and get_transfer_detail reassembles them."""
        self.db.save_transfer("u1", ALICE, None, "50", "Amulet", "debit")
        self.db.save_transfer("u1", None, BOB, "25", "Amulet", "credit")
        self.db.save_transfer("u1", None, ALICE, "25", "Amulet", "credit")
        legs = self.db.get_transfer_detail("u1")
        self.assertEqual(len(legs), 3)
        self.assertCountEqual([leg["transfer_kind"] for leg in legs],
                              ["debit", "credit", "credit"])

    def test_status_defaults_to_settled_and_can_be_set(self):
        self.db.save_transfer("u1", ALICE, BOB, "1", "Amulet", "direct")
        self.assertEqual(self.db.get_transfers(ALICE)[0]["status"], "settled")
        self.db.save_transfer("u2", ALICE, BOB, "2", "Amulet", "offer",
                              status="pending", contract_id="ti-1")
        pending = [r for r in self.db.get_transfers(ALICE)
                   if r["update_id"] == "u2"]
        self.assertEqual(pending[0]["status"], "pending")

    def test_update_transfer_status_by_contract(self):
        self.db.save_transfer("u2", ALICE, BOB, "2", "Amulet", "offer",
                              status="pending", contract_id="ti-1")
        updated = self.db.update_transfer_status_by_contract("ti-1", "resolved")
        self.assertEqual(updated, 1)
        self.assertEqual(self.db.get_transfers(ALICE)[0]["status"], "resolved")
        # An unknown contract id updates nothing and does not raise.
        self.assertEqual(
            self.db.update_transfer_status_by_contract("nope", "resolved"), 0)


class StaleTransferTests(StoreTestCase):
    """P8: 'every row marked pending must resolve within N seconds'."""

    def _pending(self, contract_id, age_seconds):
        recorded = (datetime.now(timezone.utc)
                    - timedelta(seconds=age_seconds)).isoformat()
        self.db.conn.execute(
            "INSERT INTO transfers (update_id, contract_id, sender, receiver, "
            "amount, instrument, transfer_kind, status, recorded_at) "
            "VALUES (?, ?, ?, ?, '10', 'Amulet', 'offer', 'pending', ?)",
            (f"u-{contract_id}", contract_id, ALICE, BOB, recorded),
        )
        self.db.conn.commit()

    def test_fresh_pending_transfer_is_not_stale(self):
        self._pending("ti-fresh", age_seconds=5)
        self.assertEqual(self.db.get_stale_transfers(), [])

    def test_old_pending_transfer_is_stale(self):
        self._pending("ti-old", age_seconds=DEFAULT_STALE_SECONDS + 60)
        stale = self.db.get_stale_transfers()
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["contract_id"], "ti-old")
        self.assertGreater(stale[0]["age_seconds"], DEFAULT_STALE_SECONDS)

    def test_threshold_is_a_parameter_not_a_hardcoded_number(self):
        self._pending("ti-60s", age_seconds=60)
        self.assertEqual(self.db.get_stale_transfers(), [])           # default 300
        self.assertEqual(len(self.db.get_stale_transfers(30)), 1)     # per-call
        # ... and settable per instance.
        self.db.close()
        tight = ScannerDB(self.db_path, stale_seconds=30)
        try:
            self.assertEqual(len(tight.get_stale_transfers()), 1)
            self.assertEqual(tight.stale_seconds, 30)
        finally:
            tight.close()
            self.db = ScannerDB(self.db_path)  # so tearDown has something to close

    def test_settled_transfers_are_never_stale(self):
        self.db.save_transfer("u1", ALICE, BOB, "1", "Amulet", "direct")
        self.assertEqual(self.db.get_stale_transfers(0), [])

    def test_resolved_offer_drops_out_of_the_stale_list(self):
        self._pending("ti-old", age_seconds=DEFAULT_STALE_SECONDS + 60)
        self.assertEqual(len(self.db.get_stale_transfers()), 1)
        self.db.update_transfer_status_by_contract("ti-old", "resolved")
        self.assertEqual(self.db.get_stale_transfers(), [])


class BalanceHistoryTests(StoreTestCase):
    def test_history_replays_events_into_a_running_balance(self):
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "10")
        self.db.save_event("10", "created", "a-50", party_id=ALICE)
        self.db.save_holding("a-30", ALICE, "30", "Amulet", DSO, False, "11")
        self.db.save_event("11", "created", "a-30", party_id=ALICE)
        self.db.archive_holding("a-50", "12")
        self.db.save_event("12", "archived", "a-50", party_id=ALICE)

        history = self.db.get_balance_history(ALICE)
        self.assertEqual([p["balance"] for p in history], [50.0, 80.0, 30.0])
        self.assertEqual([p["offset"] for p in history], ["10", "11", "12"])


class HealthTests(StoreTestCase):
    def test_health_on_a_fresh_database(self):
        health = self.db.get_health()
        self.assertEqual(health["status"], "no_data")
        self.assertIsNone(health["scanner_offset"])
        self.assertEqual(health["tracked_parties"], 0)
        self.assertEqual(health["stale_pending_transfers"], 0)

    def test_health_reports_counts_and_drift(self):
        self._seed()
        health = self.db.get_health("14")
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["scanner_offset"], "10")
        self.assertEqual(health["ledger_offset"], "14")
        self.assertEqual(health["scanner_delay_offsets"], 4)
        self.assertEqual(health["active_holdings"], 4)
        self.assertEqual(health["archived_holdings"], 0)
        self.assertEqual(health["total_events"], 4)
        self.assertEqual(health["tracked_parties"], 2)
        self.assertIsNotNone(health["last_updated"])

    def test_drift_is_null_when_an_offset_is_not_numeric(self):
        """Opaque string offsets must report unknown, not a nonsense number."""
        self._seed()
        self.assertIsNone(self.db.get_health("offset-abc")["scanner_delay_offsets"])
        self.assertIsNone(self.db.get_health(None)["scanner_delay_offsets"])

    def test_health_counts_stale_pending_transfers(self):
        self._seed()
        recorded = (datetime.now(timezone.utc)
                    - timedelta(seconds=DEFAULT_STALE_SECONDS + 60)).isoformat()
        self.db.conn.execute(
            "INSERT INTO transfers (update_id, sender, receiver, amount, "
            "instrument, transfer_kind, status, recorded_at) "
            "VALUES ('u1', ?, ?, '10', 'Amulet', 'offer', 'pending', ?)",
            (ALICE, BOB, recorded))
        self.db.conn.commit()
        self.assertEqual(self.db.get_health()["stale_pending_transfers"], 1)


class MetricsTests(StoreTestCase):
    def test_metrics_keeps_volume_per_instrument(self):
        """P9: different instruments are never collapsed into one number."""
        self._seed()
        self.db.save_transfer("u1", ALICE, BOB, "25", "Amulet", "direct")
        self.db.save_transfer("u2", ALICE, BOB, "5", "Amulet", "direct")
        self.db.save_transfer("u3", ALICE, BOB, "2", "c8BTC", "direct")

        metrics = self.db.get_metrics("14")
        self.assertEqual(metrics["total_transfers"], 3)
        volume = {v["instrument"]: v for v in metrics["volume_by_instrument"]}
        self.assertAlmostEqual(volume["Amulet"]["volume"], 30.0)
        self.assertEqual(volume["Amulet"]["count"], 2)
        self.assertAlmostEqual(volume["c8BTC"]["volume"], 2.0)
        self.assertEqual(metrics["tracked_parties"], 2)
        self.assertEqual(metrics["active_holdings"], 4)
        self.assertEqual(metrics["scanner_delay_offsets"], 4)

    def test_metrics_keeps_locked_totals_per_instrument(self):
        self._seed()
        self.db.save_holding("a-btc-lock", ALICE, "3", "c8BTC", DSO, True, "10")
        locked = {row["instrument"]: row
                  for row in self.db.get_metrics()["locked_by_instrument"]}
        self.assertAlmostEqual(locked["Amulet"]["locked_total"], 20.0)
        self.assertAlmostEqual(locked["c8BTC"]["locked_total"], 3.0)

    def test_metrics_drift_is_null_without_a_live_offset(self):
        self._seed()
        self.assertIsNone(self.db.get_metrics()["scanner_delay_offsets"])
        self.assertIsNone(self.db.get_metrics()["ledger_offset"])


class EventTests(StoreTestCase):
    def test_events_are_appended_with_raw_payload(self):
        self.db.save_event("10", "created", "cid-1", template_id="iface",
                           party_id=ALICE, raw_data={"amount": "5"})
        row = self.db.conn.execute("SELECT * FROM ledger_events").fetchone()
        self.assertEqual(row["event_type"], "created")
        self.assertEqual(row["template_id"], "iface")
        self.assertIn("amount", row["raw_data"])


if __name__ == "__main__":
    unittest.main()
