"""Contract tests for the local JSON API: the exact response shapes the
frontend depends on, plus the data_mode field, plus the demo seed.

WHY THIS FILE EXISTS
--------------------
``tests/test_webapi.py`` already covers routing, status codes and values. It
did not pin the *envelope* — and that is precisely what broke the frontend:
``/parties`` returns ``{"parties": [...]}``, not a bare list, and the dashboard
was testing ``Array.isArray(wholeResponse)``, which is false for an object. The
result was a UI that rendered "No parties" forever against a fully populated,
correctly-running backend, with every request returning HTTP 200.

So these tests assert the container type, the key name, and the type of what is
inside it. If anyone ever flattens a route to a bare array (a defensible change
— but one that has to be made deliberately), these fail and point at
``scanner-frontend/src/api.js`` and ``docs/ENDPOINT_DATA_MAP.md`` as the other
two places that must change with it.

Everything here is offline: an ephemeral-port ThreadingHTTPServer, and either
no ledger client or a fake one.
"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from _fakes import make_config  # noqa: F401 - ensures src/ is on sys.path

from scandex_api.demo_data import ALICE, BOB, CAROL, seed_demo_data
from scandex_api.store import ScannerDB
from scandex_api.webapi import make_server


class ContractTestCase(unittest.TestCase):
    """Serves the real deterministic demo dataset over the real server."""

    data_mode = "test"
    ledger = None

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = ScannerDB(Path(self.tmp.name) / "scandex-test.db")
        seed_demo_data(self.db)
        self.httpd = make_server(self.db, host="127.0.0.1", port=0,
                                 ledger=self.ledger, data_mode=self.data_mode)
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
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

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())


class ResponseShapeTests(ContractTestCase):
    """The three envelopes the dashboard unwraps (the Bug B regression)."""

    def test_parties_is_an_object_wrapping_a_list(self):
        status, body = self.get("/parties")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict, "/parties must return an object")
        self.assertNotIsInstance(body, list)
        self.assertIn("parties", body)
        self.assertIsInstance(body["parties"], list)
        self.assertEqual(len(body["parties"]), 3)
        # The fields the party selector actually reads.
        row = body["parties"][0]
        for field in ("party_id", "display_name", "is_local"):
            self.assertIn(field, row)

    def test_balance_is_an_object_wrapping_byInstrument(self):
        status, body = self.get("/tokens/balance/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertEqual(body["party"], ALICE)
        self.assertIn("byInstrument", body)
        self.assertIsInstance(body["byInstrument"], list)
        rows = {r["instrument"]: r for r in body["byInstrument"]}
        self.assertIn("Amulet", rows)
        # The spendable-vs-locked split (P2) the demo seed is built to show.
        self.assertAlmostEqual(rows["Amulet"]["total"], 100.0)
        self.assertAlmostEqual(rows["Amulet"]["spendable"], 80.0)
        self.assertEqual(rows["Amulet"]["locked_count"], 1)
        # A second instrument, never summed into the first.
        self.assertAlmostEqual(rows["c8BTC"]["total"], 2.0)

    def test_transfers_is_an_object_wrapping_a_list_and_a_count(self):
        status, body = self.get("/tokens/transfers/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertIsInstance(body, dict)
        self.assertEqual(body["party"], ALICE)
        self.assertIn("transfers", body)
        self.assertIsInstance(body["transfers"], list)
        # `count` is metadata a bare array would have lost - the reason the
        # envelope was kept rather than flattened.
        self.assertEqual(body["count"], len(body["transfers"]))
        self.assertEqual(body["count"], 5)

    def test_holdings_is_an_object_wrapping_a_list(self):
        status, body = self.get("/tokens/holdings/" + quote(ALICE))
        self.assertEqual(status, 200)
        self.assertIsInstance(body["holdings"], list)
        self.assertTrue(body["activeOnly"])

    def test_stale_is_an_object_wrapping_a_list_and_a_threshold(self):
        status, body = self.get("/tokens/transfers/stale")
        self.assertEqual(status, 200)
        self.assertIsInstance(body["transfers"], list)
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["olderThanSeconds"], 300)
        self.assertEqual(body["transfers"][0]["update_id"], "demo-u4")

    def test_health_and_metrics_are_flat_not_wrapped(self):
        """The dashboard reads these as-is, so they must NOT gain an envelope."""
        for path, key in (("/health", "status"), ("/metrics", "total_transfers")):
            _, body = self.get(path)
            self.assertIsInstance(body, dict)
            self.assertIn(key, body, f"{path} lost its flat shape")


class TransferFilterTests(ContractTestCase):
    """The dashboard sends ?instrument= and ?direction=; the server must
    actually apply them. Before this branch it silently ignored both."""

    def test_direction_sent_returns_only_outgoing(self):
        _, body = self.get("/tokens/transfers/" + quote(ALICE) + "?direction=sent")
        self.assertEqual(body["count"], 3)
        for t in body["transfers"]:
            self.assertEqual(t["sender"], ALICE)

    def test_direction_received_returns_only_incoming(self):
        _, body = self.get("/tokens/transfers/" + quote(ALICE) + "?direction=received")
        self.assertEqual(body["count"], 2)
        for t in body["transfers"]:
            self.assertEqual(t["receiver"], ALICE)

    def test_instrument_filter(self):
        _, body = self.get("/tokens/transfers/" + quote(ALICE) + "?instrument=c8BTC")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["transfers"][0]["instrument"], "c8BTC")

    def test_filters_combine(self):
        _, body = self.get(
            "/tokens/transfers/" + quote(ALICE) + "?instrument=Amulet&direction=sent")
        self.assertEqual(body["count"], 3)

    def test_unknown_direction_degrades_to_both(self):
        """A stray value must not silently return an empty list."""
        _, body = self.get("/tokens/transfers/" + quote(ALICE) + "?direction=sideways")
        self.assertEqual(body["count"], 5)

    def test_filtering_happens_before_limit(self):
        """?limit=2&instrument=c8BTC must return the c8BTC row, not "the newest
        2 transfers, of which none are c8BTC"."""
        _, body = self.get(
            "/tokens/transfers/" + quote(ALICE) + "?limit=2&instrument=c8BTC")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["transfers"][0]["instrument"], "c8BTC")


class DataModeTests(ContractTestCase):
    """data_mode must be reported by the server on both /health and /, in both
    modes, so a frontend never has to infer it from the port."""

    def test_health_reports_test_mode(self):
        _, body = self.get("/health")
        self.assertEqual(body["data_mode"], "test")

    def test_index_reports_test_mode(self):
        _, body = self.get("/")
        self.assertEqual(body["dataMode"], "test")
        self.assertTrue(body["readOnly"])


class RealModeDataModeTests(ContractTestCase):
    """The same, with the server built in real mode and no ledger client -
    equivalent to `serve_api.py --data-mode real --no-ledger`."""

    data_mode = "real"

    def test_health_reports_real_mode(self):
        _, body = self.get("/health")
        self.assertEqual(body["data_mode"], "real")
        # --no-ledger: drift is null, and says why, rather than failing.
        self.assertIsNone(body["ledger_offset"])
        self.assertIn("ledger_offset_note", body)

    def test_index_reports_real_mode(self):
        _, body = self.get("/")
        self.assertEqual(body["dataMode"], "real")

    def test_real_mode_still_serves_whatever_is_in_the_database(self):
        """Real mode must not switch which file it reads. This server was given
        a demo-seeded database and must serve exactly that, unchanged."""
        _, body = self.get("/parties")
        self.assertEqual(len(body["parties"]), 3)


class DemoSeedTests(unittest.TestCase):
    """The demo dataset itself: deterministic, idempotent, and offline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = ScannerDB(Path(self.tmp.name) / "seed.db")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_seed_is_idempotent(self):
        """Restarting the server re-seeds; it must rebuild the same dataset,
        not stack a second copy on top."""
        first = seed_demo_data(self.db)
        counts_1 = self.db.get_health()
        second = seed_demo_data(self.db)
        counts_2 = self.db.get_health()
        self.assertEqual(first, second)
        self.assertEqual(counts_1["total_transfers"], counts_2["total_transfers"])
        self.assertEqual(counts_1["tracked_parties"], counts_2["tracked_parties"])
        self.assertEqual(counts_2["tracked_parties"], 3)

    def test_seed_produces_the_spendable_locked_split(self):
        seed_demo_data(self.db)
        rows = {r["instrument"]: r for r in self.db.get_balance(ALICE)}
        self.assertAlmostEqual(rows["Amulet"]["total"], 100.0)
        self.assertAlmostEqual(rows["Amulet"]["spendable"], 80.0)

    def test_seed_produces_exactly_one_stale_pending_offer(self):
        seed_demo_data(self.db)
        stale = self.db.get_stale_transfers()
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["update_id"], "demo-u4")
        self.assertEqual(stale[0]["status"], "pending")

    def test_seed_saves_a_checkpoint_so_health_is_ok_not_no_data(self):
        seed_demo_data(self.db)
        self.assertEqual(self.db.get_health()["status"], "ok")

    def test_seed_respects_a_custom_stale_threshold(self):
        """A server started with --stale-seconds must still see the demo offer
        as stale, or the P8 demo silently shows nothing."""
        db = ScannerDB(Path(self.tmp.name) / "custom.db", stale_seconds=3600)
        try:
            seed_demo_data(db)
            self.assertEqual(len(db.get_stale_transfers()), 1)
        finally:
            db.close()

    def test_seed_records_all_three_parties(self):
        seed_demo_data(self.db)
        ids = [p["party_id"] for p in self.db.get_parties()]
        self.assertCountEqual(ids, [ALICE, BOB, CAROL])
        local = [p for p in self.db.get_parties() if p["is_local"]]
        self.assertEqual([p["party_id"] for p in local], [ALICE])


class TestModeIsOfflineTests(unittest.TestCase):
    """Test mode must never contact the ledger.

    Rather than trusting a comment, this asserts the structural property that
    makes it true: main() never constructs a ledger client in test mode, so
    there is no object on which a network call could be made.
    """

    def test_main_does_not_build_a_ledger_client_in_test_mode(self):
        import scandex_api.webapi as webapi

        calls = []
        original = webapi.build_ledger_client

        def spy(*a, **kw):
            calls.append((a, kw))
            raise AssertionError(
                "test mode must not build a ledger client - it is what "
                "guarantees --data-mode test makes no network call")

        webapi.build_ledger_client = spy
        served = {}

        def fake_serve(db, **kwargs):
            served.update(kwargs)
            served["parties"] = len(db.get_parties())

        original_serve = webapi.serve
        webapi.serve = fake_serve
        tmp = tempfile.TemporaryDirectory()
        try:
            rc = webapi.main([
                "--data-mode", "test",
                "--db", str(Path(tmp.name) / "t.db"),
            ])
        finally:
            webapi.build_ledger_client = original
            webapi.serve = original_serve
            tmp.cleanup()

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [], "build_ledger_client was called in test mode")
        self.assertIsNone(served["ledger"])
        self.assertEqual(served["data_mode"], "test")
        # And it really did seed the demo dataset.
        self.assertEqual(served["parties"], 3)

    def test_db_path_defaults_per_mode(self):
        """Seeding demo data must never be able to overwrite a real indexed
        database, so the two modes default to different files."""
        import scandex_api.webapi as webapi

        parser = webapi.build_parser()
        self.assertIsNone(parser.parse_args([]).db)
        self.assertEqual(parser.parse_args([]).data_mode, "real")
        self.assertEqual(parser.parse_args(["--data-mode", "test"]).data_mode, "test")


if __name__ == "__main__":
    unittest.main()
