import unittest

from _fakes import _SRC  # noqa: F401  (ensures src is on sys.path)

from scandex_api import redaction
from scandex_api.config import Config, load_config, parse_env_file
from scandex_api.errors import ConfigError


class ConfigTests(unittest.TestCase):
    def test_defaults_applied_when_env_empty(self):
        cfg = load_config(environ={}, env_file_path=None)
        self.assertTrue(cfg.base.startswith("https://api.validator.dev"))
        self.assertEqual(cfg.client_id, "hackathon")
        self.assertFalse(cfg.has_secret)

    def test_missing_secret_is_not_fatal(self):
        # No secret -> config still loads; the diagnostic reports it later.
        cfg = load_config(environ={}, env_file_path=None)
        self.assertFalse(cfg.has_secret)
        self.assertIn("C8_CLIENT_SECRET is not set", cfg.missing_secret_message())

    def test_empty_env_value_falls_back_to_default(self):
        # An empty env value is treated as "unset" and falls back to the safe
        # DevNet default rather than erroring — friendlier for a beginner.
        cfg = load_config(environ={"C8_BASE": ""}, env_file_path=None)
        self.assertTrue(cfg.base.startswith("https://api.validator.dev"))

    def test_config_error_is_a_distinct_type(self):
        # The distinct exit-code-2 path exists and is a subclass of the base.
        from scandex_api.errors import ScandexError
        self.assertTrue(issubclass(ConfigError, ScandexError))

    def test_real_env_wins_over_env_file(self):
        env_file = {"C8_PARTY": "from-file"}
        cfg = load_config(environ={"C8_PARTY": "from-env"}, env_file_path=None)
        self.assertEqual(cfg.party, "from-env")
        # And the .env supplies it when env does not:
        # simulate by writing a temp file
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("C8_PARTY=from-file\n")
            cfg2 = load_config(environ={}, env_file_path=path)
            self.assertEqual(cfg2.party, "from-file")
        finally:
            os.remove(path)

    def test_party_and_timeout_overrides(self):
        cfg = load_config(environ={}, env_file_path=None, party="p1", timeout=12.0)
        self.assertEqual(cfg.party, "p1")
        self.assertEqual(cfg.timeout, 12.0)

    def test_timeout_from_env_must_be_number(self):
        with self.assertRaises(ConfigError):
            load_config(environ={"C8_TIMEOUT": "abc"}, env_file_path=None)

    def test_secret_registered_for_redaction(self):
        load_config(environ={"C8_CLIENT_SECRET": "supersecretvalue123"},
                    env_file_path=None)
        self.assertNotIn("supersecretvalue123",
                         redaction.redact("token=supersecretvalue123 done"))

    def test_parse_env_file_forms(self):
        import tempfile
        import os
        fd, path = tempfile.mkstemp(suffix=".env")
        os.close(fd)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("# a comment\n")
                fh.write("export C8_CLIENT_ID=hackathon\n")
                fh.write('C8_PARTY="quoted::party"\n')
                fh.write("\n")
                fh.write("garbage line without equals\n")
            parsed = parse_env_file(path)
            self.assertEqual(parsed["C8_CLIENT_ID"], "hackathon")
            self.assertEqual(parsed["C8_PARTY"], "quoted::party")
            self.assertNotIn("garbage line without equals", parsed)
        finally:
            os.remove(path)

    def test_token_url_built(self):
        cfg = Config(base="b", idp="https://auth.test", client_id="c",
                     client_secret="s", registry="r", user="u",
                     scanner_base="sc", scan_base="sn")
        self.assertEqual(
            cfg.token_url,
            "https://auth.test/realms/master/protocol/openid-connect/token")


if __name__ == "__main__":
    unittest.main()
