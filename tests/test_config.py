"""Unit tests for configuration loading and validation."""

import os
import tempfile
import unittest
from pathlib import Path

from butler.config import load_env_file, load_config, deep_merge


class TestConfig(unittest.TestCase):
    def setUp(self):
        self.orig_environ = os.environ.copy()
        # Clean specific test keys to isolate tests
        for key in ["VIKUNJA_URL", "VIKUNJA_TOKEN", "BUTLER_TIMEZONE", "TEST_ENV_VAR"]:
            if key in os.environ:
                del os.environ[key]

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.orig_environ)

    def test_load_env_file(self):
        test_url = "http://127.0.0.1:3456"
        with tempfile.NamedTemporaryFile("w", delete=False) as tf:
            tf.write("VIKUNJA_URL=" + test_url + "\n")
            tf.write("VIKUNJA_TOKEN=secret_token_abc\n")
            tf.write("TEST_ENV_VAR=hello_world\n")
            tf.write("# Comment line\n")
            tf.write("EMPTY_VAL=\n")
            env_path = tf.name

        try:
            load_env_file(env_path, override=True)
            self.assertEqual(os.environ.get("VIKUNJA_URL"), test_url)
            self.assertEqual(os.environ.get("VIKUNJA_TOKEN"), "secret_token_abc")
            self.assertEqual(os.environ.get("TEST_ENV_VAR"), "hello_world")
        finally:
            if os.path.exists(env_path):
                os.remove(env_path)

    def test_deep_merge(self):
        base = {"a": 1, "nested": {"x": 10, "y": 20}}
        override = {"nested": {"y": 99, "z": 100}, "b": 2}
        res = deep_merge(base, override)
        self.assertEqual(res["a"], 1)
        self.assertEqual(res["b"], 2)
        self.assertEqual(res["nested"]["x"], 10)
        self.assertEqual(res["nested"]["y"], 99)
        self.assertEqual(res["nested"]["z"], 100)

    def test_load_config_with_env_overrides(self):
        override_url = "http://127.0.0.1:8080"
        os.environ["VIKUNJA_URL"] = override_url
        os.environ["VIKUNJA_TOKEN"] = "token_override_123"
        os.environ["BUTLER_TIMEZONE"] = "UTC"

        cfg = load_config(config_path="/non_existent_file.yaml")
        self.assertEqual(cfg["vikunja"]["url"], override_url)
        self.assertEqual(cfg["vikunja"]["token"], "token_override_123")
        self.assertEqual(cfg["timezone"], "UTC")
        self.assertIn(5, cfg["gtd"]["allowed_target_projects"])

    def test_load_config_with_env_file_setting(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as env_f:
            env_f.write("VIKUNJA_URL=http://env-file-instance.local\nVIKUNJA_TOKEN=env_tok_999\n")
            env_file_path = env_f.name

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as cfg_f:
            import json
            json.dump({"env_file": env_file_path, "timezone": "Asia/Shanghai"}, cfg_f)
            cfg_file_path = cfg_f.name

        try:
            cfg = load_config(config_path=cfg_file_path)
            self.assertEqual(cfg["vikunja"]["url"], "http://env-file-instance.local")
            self.assertEqual(cfg["vikunja"]["token"], "env_tok_999")
            self.assertEqual(cfg["env_file"], env_file_path)
        finally:
            if os.path.exists(env_file_path):
                os.remove(env_file_path)
            if os.path.exists(cfg_file_path):
                os.remove(cfg_file_path)


if __name__ == "__main__":
    unittest.main()
