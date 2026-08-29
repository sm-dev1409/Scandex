import unittest

from _fakes import FakeTransport, make_config

from scandex_api.http import HttpClient
from scandex_api.registry import RegistryClient


def _wire(transport, **cfg_overrides):
    cfg = make_config(**cfg_overrides)
    http = HttpClient(timeout=5.0, retries=0, transport=transport)
    return cfg, RegistryClient(cfg, http)


class RegistryTests(unittest.TestCase):
    def test_info_readable(self):
        t = FakeTransport().add("GET", "/registry/metadata/v1/info",
                                json_body={"adminId": "DSO::1",
                                           "supportedApis": ["transfer"]})
        _, reg = _wire(t)
        info = reg.info()
        self.assertEqual(info["adminId"], "DSO::1")

    def test_instruments_parsed(self):
        t = FakeTransport().add("GET", "/registry/metadata/v1/instruments",
                                json_body={"instruments": [
                                    {"id": "Amulet", "name": "Canton Coin",
                                     "admin": "DSO::1", "decimals": 10},
                                    {"id": "c8ETH", "administrator": "c8::1",
                                     "decimals": 18},
                                ]})
        _, reg = _wire(t)
        instruments = reg.instruments()
        self.assertEqual(len(instruments), 2)
        self.assertEqual(instruments[0].id, "Amulet")
        self.assertEqual(instruments[0].decimals, 10)
        self.assertEqual(instruments[1].administrator, "c8::1")

    def test_host_header_sent_when_configured(self):
        t = FakeTransport().add("GET", "/registry/metadata/v1/info",
                                json_body={"adminId": "x"})
        _, reg = _wire(t, registry_host="scan.localhost")
        reg.info()
        headers = t.calls[0]["headers"]
        self.assertEqual(headers.get("Host"), "scan.localhost")

    def test_choice_context_meta_is_flat_map(self):
        # The shape trap: meta must be {} not {"values": {}}.
        t = FakeTransport().add(
            "POST", "/choice-contexts/accept",
            json_body={"choiceContextData": {}, "disclosedContracts": []})
        _, reg = _wire(t)
        reg.accept_context("cid-123")
        body = t.bodies_sent("/choice-contexts/accept")[0]
        self.assertEqual(body, {"meta": {}})

    def test_transfer_factory_preview_wraps_choice_arguments(self):
        t = FakeTransport().add(
            "POST", "/transfer-instruction/v1/transfer-factory",
            json_body={"factoryId": "f1", "transferKind": "direct",
                       "choiceContext": {}})
        _, reg = _wire(t)
        out = reg.transfer_factory_preview({"expectedAdmin": "DSO::1"})
        self.assertEqual(out["transferKind"], "direct")
        body = t.bodies_sent("/transfer-factory")[0]
        self.assertIn("choiceArguments", body)


if __name__ == "__main__":
    unittest.main()
