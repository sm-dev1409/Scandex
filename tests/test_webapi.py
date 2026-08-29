"""Offline tests for the local JSON API (scandex_api.webapi).

A real ThreadingHTTPServer is started on an ephemeral port in a background
thread and driven with urllib, so the routing, status codes, JSON bodies and
CORS header are all exercised end to end. Nothing here touches the network
beyond 127.0.0.1, and the ledger client is always a fake or None.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from _fakes import make_config  # noqa: F401 - ensures src/ is on sys.path

from scandex_api.errors import UnreachableError
from scandex_api.store import DEFAULT_STALE_SECONDS, ScannerDB
from scandex_api.webapi import make_server

ALICE = "alice::1"
BOB = "bob::1"
DSO = "DSO::1"


class FakeLedger:
    """Stands in for LedgerClient.ledger_end() only."""

    def __init__(self, offset="140", raises=None):
        self.offset = offset
        self.raises = raises
        self.calls = 0

    def ledger_end(self):
        self.calls += 1
        if self.raises:
            raise self.raises
        return self.offset


class WebApiTestCase(unittest.TestCase):
    ledger = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = ScannerDB(Path(self.tmp.name) / "scandex.db")
        self.seed()
        self.httpd = make_server(self.db, host="127.0.0.1", port=0,
                                 ledger=self.ledger)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        # A short poll interval only so tearDown's shutdown() returns promptly;
        # serve_forever's 0.5s default would add half a second per test.
        self.thread = threading.Thread(
            target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02},
            daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=5)
        self.httpd.server_close()
        self.db.close()
        self.tmp.cleanup()

    def seed(self):
        """Alice: 50 + 30 + 20(locked) Amulet. Bob: 75. One settled transfer,
        one long-pending offer."""
        self.db.save_party(ALICE, display_name="Alice", is_local=True)
        self.db.save_party(BOB, display_name="Bob", is_local=False)
        self.db.save_holding("a-50", ALICE, "50", "Amulet", DSO, False, "10")
        self.db.save_holding("a-30", ALICE, "30", "Amulet", DSO, False, "10")
        self.db.save_holding("a-20", ALICE, "20", "Amulet", DSO, True, "10")
        self.db.save_holding("b-75", BOB, "75", "Amulet", DSO, False, "10")
        self.db.save_event("10", "created", "a-50", party_id=ALICE)
        self.db.save_transfer("u1", ALICE, BOB, "25", "Amulet", "direct",
                              ledger_offset="10")
        stale_at = (datetime.now(timezone.utc)
                    - timedelta(seconds=DEFAULT_STALE_SECONDS + 60)).isoformat()
        self.db.conn.execute(
            "INSERT INTO transfers (update_id, contract_id, sender, receiver, "
            "amount, instrument, transfer_kind, status, recorded_at) "
            "VALUES ('u2', 'ti-1', ?, ?, '5', 'Amulet', 'offer', 'pending', ?)",
            (ALICE, BOB, stale_at))
        self.db.conn.commit()
        self.db.save_offset("10")

    # -- helpers ----------------------------------------------------------

    def get(self, path):
        """GET a path, returning (status, parsed_json, headers). A 4xx comes
        back as a value, not an exception, so error bodies can be asserted."""
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read()), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read()), dict(exc.headers)


class RouteTests(WebApiTestCase):
    def test_health(self):
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["scanner_offset"], "10")
        self.assertEqual(body["active_holdings"], 4)
        self.assertEqual(body["tracked_parties"], 2)
        self.assertEqual(body["stale_pending_transfers"], 1)
        # No ledger client wired in this case, so drift is null with a reason.
        self.assertIsNone(body["ledger_offset"])
        self.assertIn("ledger_offset_note", body)

    def test_parties(self):
        status, body, _ = self.get("/parties")
        self.assertEqual(status, 200)
        self.assertCountEqual([p["party_id"] for p in body["parties"]],
                              [ALICE, BOB])
        self.assertEqual(body["parties"][0]["display_name"], "Alice")

    def test_balance(self):
        status, body, _ = self.get("/tokens/balance/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertEqual(body["party"], ALICE)
        row = body["byInstrument"][0]
        self.assertEqual(row["instrument"], "Amulet")
        self.assertAlmostEqual(row["total"], 100.0)
        self.assertAlmostEqual(row["spendable"], 80.0)

    def test_balance_filtered_by_instrument(self):
        status, body, _ = self.get(
            "/tokens/balance/" + quote(ALICE) + "?instrument=c8BTC")
        self.assertEqual(status, 200)
        self.assertEqual(body["byInstrument"], [])

    def test_holdings(self):
        status, body, _ = self.get("/tokens/holdings/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertEqual(len(body["holdings"]), 3)
        self.assertTrue(body["activeOnly"])
        locked = [h for h in body["holdings"] if h["locked"]]
        self.assertEqual(len(locked), 1)
        self.assertEqual(locked[0]["contract_id"], "a-20")

    def test_holdings_can_include_archived(self):
        self.db.archive_holding("a-50", "11")
        status, body, _ = self.get(
            "/tokens/holdings/" + quote(ALICE) + "?active_only=0")
        self.assertEqual(status, 200)
        self.assertFalse(body["activeOnly"])
        self.assertEqual(len(body["holdings"]), 3)

    def test_transfers(self):
        status, body, _ = self.get("/tokens/transfers/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)
        kinds = {t["transfer_kind"] for t in body["transfers"]}
        self.assertEqual(kinds, {"direct", "offer"})

    def test_transfers_limit(self):
        status, body, _ = self.get(
            "/tokens/transfers/" + quote(ALICE) + "?limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)

    def test_stale_transfers(self):
        status, body, _ = self.get("/tokens/transfers/stale")
        self.assertEqual(status, 200)
        self.assertEqual(body["olderThanSeconds"], DEFAULT_STALE_SECONDS)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["transfers"][0]["contract_id"], "ti-1")
        self.assertEqual(body["transfers"][0]["status"], "pending")

    def test_stale_transfers_threshold_is_overridable(self):
        status, body, _ = self.get(
            "/tokens/transfers/stale?older_than_seconds=100000")
        self.assertEqual(status, 200)
        self.assertEqual(body["olderThanSeconds"], 100000)
        self.assertEqual(body["count"], 0)

    def test_stale_route_is_not_read_as_a_party_id(self):
        """/tokens/transfers/stale must not fall through to the {party} form."""
        status, body, _ = self.get("/tokens/transfers/stale")
        self.assertEqual(status, 200)
        self.assertIn("olderThanSeconds", body)
        self.assertNotIn("party", body)

    def test_owners(self):
        status, body, _ = self.get("/tokens/owners")
        self.assertEqual(status, 200)
        totals = {o["party_id"]: o["total"] for o in body["owners"]}
        self.assertAlmostEqual(totals[ALICE], 100.0)
        self.assertAlmostEqual(totals[BOB], 75.0)

    def test_metrics(self):
        status, body, _ = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertEqual(body["total_transfers"], 2)
        volume = {v["instrument"]: v["volume"] for v in body["volume_by_instrument"]}
        self.assertAlmostEqual(volume["Amulet"], 30.0)  # 25 settled + 5 offer
        locked = {v["instrument"]: v["locked_total"]
                  for v in body["locked_by_instrument"]}
        self.assertAlmostEqual(locked["Amulet"], 20.0)
        self.assertEqual(body["stale_pending_transfers"], 1)
        self.assertIsNone(body["scanner_delay_offsets"])

    def test_index_lists_the_routes(self):
        status, body, _ = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("/health", body["routes"])
        self.assertIn("/tokens/balance/{party}", body["routes"])


class ErrorTests(WebApiTestCase):
    def test_unknown_route_is_a_json_404(self):
        status, body, headers = self.get("/nope")
        self.assertEqual(status, 404)
        self.assertIn("error", body)
        self.assertIn("no such route", body["error"])
        self.assertIn("routes", body)
        self.assertIn("application/json", headers["Content-Type"])

    def test_unknown_nested_route_is_a_json_404(self):
        status, body, _ = self.get("/tokens/nope/x")
        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_unknown_party_is_a_json_404(self):
        status, body, _ = self.get("/tokens/balance/" + quote("ghost::9"))
        self.assertEqual(status, 404)
        self.assertIn("unknown party", body["error"])

    def test_known_party_with_no_data_is_an_empty_list_not_an_error(self):
        """A party the indexer has seen but which holds nothing yet is a
        successful empty answer -- the frontend renders 'no holdings', not an
        error state."""
        self.db.save_party("carol::1", display_name="Carol")
        for path, key in (("/tokens/balance/", "byInstrument"),
                          ("/tokens/holdings/", "holdings"),
                          ("/tokens/transfers/", "transfers")):
            with self.subTest(path=path):
                status, body, _ = self.get(path + quote("carol::1"))
                self.assertEqual(status, 200)
                self.assertEqual(body[key], [])

    def test_a_bad_query_parameter_does_not_500(self):
        status, body, _ = self.get(
            "/tokens/transfers/" + quote(ALICE) + "?limit=banana")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 2)  # fell back to the default

    def test_server_stays_up_after_a_bad_request(self):
        self.get("/nope")
        self.get("/tokens/balance/" + quote("ghost::9"))
        status, _, _ = self.get("/health")
        self.assertEqual(status, 200)


class CorsTests(WebApiTestCase):
    def test_cors_header_on_a_success(self):
        _, _, headers = self.get("/health")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")

    def test_cors_header_on_a_404(self):
        _, _, headers = self.get("/nope")
        self.assertEqual(headers["Access-Control-Allow-Origin"], "*")


class LiveLedgerTests(WebApiTestCase):
    ledger = None  # replaced per-test below

    def setUp(self):
        self.ledger = FakeLedger(offset="140")
        super().setUp()

    def test_health_reports_real_drift_when_the_ledger_answers(self):
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["ledger_offset"], "140")
        self.assertEqual(body["scanner_delay_offsets"], 130)  # 140 - 10
        self.assertNotIn("ledger_offset_note", body)
        self.assertEqual(self.ledger.calls, 1)

    def test_metrics_reports_real_drift(self):
        _, body, _ = self.get("/metrics")
        self.assertEqual(body["ledger_offset"], "140")
        self.assertEqual(body["scanner_delay_offsets"], 130)

    def test_ledger_end_is_read_per_request(self):
        self.get("/health")
        self.get("/health")
        self.assertEqual(self.ledger.calls, 2)


class DegradedLedgerTests(WebApiTestCase):
    """DevNet down must not take the API down with it."""

    def setUp(self):
        self.ledger = FakeLedger(raises=UnreachableError("could not reach ledger"))
        super().setUp()

    def test_health_still_answers_with_a_null_offset_and_a_note(self):
        status, body, _ = self.get("/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertIsNone(body["ledger_offset"])
        self.assertIsNone(body["scanner_delay_offsets"])
        self.assertIn("unreachable", body["ledger_offset_note"])
        # The purely local numbers are still correct.
        self.assertEqual(body["active_holdings"], 4)

    def test_other_routes_are_unaffected(self):
        status, body, _ = self.get("/tokens/balance/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["byInstrument"][0]["total"], 100.0)


class NonNumericOffsetTests(WebApiTestCase):
    """Offsets are opaque strings on some deployments: report unknown, never a
    nonsense subtraction."""

    def setUp(self):
        self.ledger = FakeLedger(offset="opaque-offset-abc")
        super().setUp()

    def test_drift_is_null_rather_than_a_bogus_number(self):
        _, body, _ = self.get("/health")
        self.assertEqual(body["ledger_offset"], "opaque-offset-abc")
        self.assertIsNone(body["scanner_delay_offsets"])


if __name__ == "__main__":
    unittest.main()
