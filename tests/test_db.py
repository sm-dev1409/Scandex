import sqlite3
import tempfile
import unittest
from pathlib import Path

from _fakes import make_config  # noqa: F401 - ensures src/ is on sys.path

from scandex_api.db import UPDATES_STREAM, Database


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "scandex.db"
        self.db = Database(self.db_path)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_schema_is_idempotent(self):
        # Re-opening the same file must not error and must not lose data.
        self.db.set_offset("77")
        self.db.close()
        again = Database(self.db_path)
        try:
            self.assertEqual(again.get_offset(), "77")
        finally:
            again.close()

    def test_offset_round_trip(self):
        self.assertIsNone(self.db.get_offset())
        self.db.set_offset(42)
        self.assertEqual(self.db.get_offset(), "42")
        self.db.set_offset(43)
        self.assertEqual(self.db.get_offset(), "43")
        # A different stream is tracked independently.
        self.db.set_offset("A", stream="other")
        self.assertEqual(self.db.get_offset(stream="other"), "A")
        self.assertEqual(self.db.get_offset(), "43")

    def test_upsert_and_archive_holding(self):
        self.db.upsert_holding(
            contract_id="cid-1", party_id="alice::1", amount="100.0",
            instrument_id="Amulet", administrator="DSO::1",
            locked=False, lock_expiry=None, read_at_offset="10",
        )
        self.db.upsert_holding(
            contract_id="cid-2", party_id="alice::1", amount="40.0",
            instrument_id="Amulet", administrator="DSO::1",
            locked=True, lock_expiry="2030-01-01T00:00:00Z", read_at_offset="10",
        )
        rows = self.db.balance_for("alice::1")
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["total"], 140.0)
        self.assertAlmostEqual(rows[0]["spendable"], 100.0)

        # Archive the unlocked one; only 40 remains as total, spendable becomes 0.
        self.db.archive_holding("cid-1", archived_at_offset="20")
        rows = self.db.balance_for("alice::1")
        self.assertAlmostEqual(rows[0]["total"], 40.0)
        self.assertAlmostEqual(rows[0]["spendable"], 0.0)

    def test_insert_transfer_is_idempotent(self):
        ok1 = self.db.insert_transfer(
            update_id="u1", sender="alice::1", receiver="bob::1",
            instrument_id="Amulet", amount="5.0",
        )
        ok2 = self.db.insert_transfer(
            update_id="u1", sender="alice::1", receiver="bob::1",
            instrument_id="Amulet", amount="5.0",
        )
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        rows = self.db.transfers_for("alice::1")
        self.assertEqual(len(rows), 1)
        # A distinct row (different amount) does insert.
        ok3 = self.db.insert_transfer(
            update_id="u1", sender="alice::1", receiver="bob::1",
            instrument_id="Amulet", amount="6.0",
        )
        self.assertTrue(ok3)


if __name__ == "__main__":
    unittest.main()
