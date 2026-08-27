import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_controller import __version__
from desk_controller.config import (
    load_config,
    save_mqtt_config,
    user_config_path,
)
from desk_controller.desktop_agent.updater import GitHubReleaseUpdater


class ConfigLoadingTests(unittest.TestCase):
    def test_environment_path_overrides_default_locations(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "desk.yaml"
            config_path.write_text(
                "mqtt:\n  broker: mqtt.example.test\n  port: 2883\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"DESK_CONTROLLER_CONFIG": str(config_path)},
                clear=False,
            ):
                config = load_config()

        self.assertEqual(config["mqtt"]["broker"], "mqtt.example.test")
        self.assertEqual(config["mqtt"]["port"], 2883)
        self.assertEqual(config["mqtt"]["username"], "")

    def test_explicit_missing_path_uses_defaults(self):
        config = load_config("/definitely/missing/desk-controller.yaml")

        self.assertEqual(config["server"]["port"], 8080)
        self.assertEqual(config["server"]["host"], "127.0.0.1")
        self.assertEqual(config["mqtt"]["broker"], "homeassistant.local")

    def test_mqtt_settings_are_persisted_without_discarding_other_config(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "DeskController" / "config.yaml"
            config_path.parent.mkdir()
            config_path.write_text(
                "hardware:\n  simulate: true\nmqtt:\n  broker: old.example.test\n",
                encoding="utf-8",
            )

            saved_path = save_mqtt_config(
                {
                    "broker": "mqtt.example.test",
                    "port": "2883",
                    "username": "desk-agent",
                    "password": "test-password",  # pragma: allowlist secret
                },
                config_path,
            )
            config = load_config(str(config_path))

            self.assertEqual(saved_path, config_path)
            self.assertTrue(config["hardware"]["simulate"])
            self.assertEqual(
                config["mqtt"],
                {
                    "mode": "external",
                    "broker": "mqtt.example.test",
                    "port": 2883,
                    "username": "desk-agent",
                    "password": "test-password",  # pragma: allowlist secret
                },
            )
            if os.name != "nt":
                self.assertEqual(
                    stat.S_IMODE(config_path.stat().st_mode),
                    0o600,
                )

    def test_invalid_mqtt_settings_are_not_written(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"

            with self.assertRaises(ValueError):
                save_mqtt_config(
                    {
                        "broker": "",
                        "port": 70000,
                        "username": "",
                        "password": "",
                    },
                    config_path,
                )

            self.assertFalse(config_path.exists())

    def test_windows_uses_appdata_for_desktop_configuration(self):
        with (
            patch("desk_controller.config.sys.platform", "win32"),
            patch.dict(
                os.environ,
                {"APPDATA": r"C:\Users\Example\AppData\Roaming"},
                clear=False,
            ),
        ):
            path = user_config_path()

        self.assertEqual(
            path,
            Path(r"C:\Users\Example\AppData\Roaming") / "DeskController" / "config.yaml",
        )

    def test_updater_uses_package_version(self):
        self.assertEqual(GitHubReleaseUpdater.CURRENT_VERSION, f"v{__version__}")


if __name__ == "__main__":
    unittest.main()
