import os
import tempfile
import unittest
from pathlib import Path

import yaml

from desk_controller.mqtt_bootstrap import ensure_local_mqtt_credentials


class LocalMQTTBootstrapTests(unittest.TestCase):
    def test_fresh_local_config_gets_random_owner_only_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "mqtt:\n  mode: local\n  username: ''\n  password: ''\n",
                encoding="utf-8",
            )

            username, password = ensure_local_mqtt_credentials(path)

            saved = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(username, "desk-controller")
            self.assertGreaterEqual(len(password), 32)
            self.assertEqual(saved["mqtt"]["broker"], "127.0.0.1")
            self.assertEqual(saved["mqtt"]["username"], username)
            self.assertEqual(saved["mqtt"]["password"], password)
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_existing_credentials_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "mqtt:\n"
                "  mode: local\n"
                "  username: existing-user\n"
                "  password: existing-password\n",
                encoding="utf-8",
            )

            credentials = ensure_local_mqtt_credentials(path)

            self.assertEqual(
                credentials,
                ("existing-user", "existing-password"),
            )

    def test_external_broker_config_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("mqtt:\n  mode: external\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "local mode"):
                ensure_local_mqtt_credentials(path)


if __name__ == "__main__":
    unittest.main()
