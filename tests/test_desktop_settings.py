import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desk_controller.config import load_config
from desk_controller.desktop_agent.main_tray import (
    DeskAgentTrayApp,
    create_tray_icon,
)
from desk_controller.desktop_agent.settings import show_mqtt_settings


class DesktopSettingsTests(unittest.TestCase):
    def test_windows_uses_native_desktop_settings_form(self):
        expected = {
            "broker": "mqtt.example.test",
            "port": 1883,
            "username": "desk",
            "password": "secret",  # pragma: allowlist secret
        }
        with (
            patch(
                "desk_controller.desktop_agent.settings.platform.system",
                return_value="Windows",
            ),
            patch(
                "desk_controller.desktop_agent.settings._show_windows_settings",
                return_value=expected,
            ) as windows_form,
        ):
            result = show_mqtt_settings(expected)

        self.assertEqual(result, expected)
        windows_form.assert_called_once_with(expected, status_info=None)

    def test_tray_settings_are_saved_and_applied_without_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = object.__new__(DeskAgentTrayApp)
            app.config_path = Path(temp_dir) / "DeskController" / "config.yaml"
            app.agent = Mock()
            app.agent.reconfigure_mqtt.return_value = True
            app.icon = None
            settings = {
                "broker": "mqtt.example.test",
                "port": 2883,
                "username": "desk-agent",
                "password": "test-password",  # pragma: allowlist secret
            }

            with patch(
                "desk_controller.desktop_agent.main_tray.show_mqtt_settings",
                return_value=settings,
            ):
                app._edit_mqtt_settings()

            self.assertEqual(
                load_config(str(app.config_path))["mqtt"],
                {"mode": "external", **settings},
            )
            app.agent.reconfigure_mqtt.assert_called_once_with(settings)

    def test_tray_connection_state_changes_icon_tooltip_and_menu(self):
        app = object.__new__(DeskAgentTrayApp)
        app.icon = Mock()
        app.agent = Mock()
        app.agent.mqtt = Mock(is_auth_failed=False)
        app._mqtt_connected = False

        app._on_connection_state_changed(True)

        self.assertTrue(app._mqtt_connected)
        self.assertEqual(app._connection_menu_text(), "● MQTT connected")
        self.assertEqual(app.icon.title, "Desk Agent — MQTT connected")
        app.icon.update_menu.assert_called_once_with()
        self.assertEqual(app.icon.icon.getpixel((32, 4)), (52, 199, 123, 255))

        app._on_connection_state_changed(False)

        self.assertEqual(
            app._connection_menu_text(),
            "● MQTT offline — reconnecting",
        )
        self.assertEqual(
            app.icon.title,
            "Desk Agent — MQTT offline, reconnecting",
        )
        self.assertNotEqual(
            create_tray_icon(False).getpixel((32, 4)),
            create_tray_icon(True).getpixel((32, 4)),
        )

        # Test auth failure state in tray
        app.agent.mqtt.is_auth_failed = True
        app._on_connection_state_changed(False)
        self.assertEqual(
            app._connection_menu_text(),
            "● MQTT auth failed (bad credentials)",
        )
        self.assertEqual(
            app.icon.title,
            "Desk Agent — MQTT authentication failed",
        )


if __name__ == "__main__":
    unittest.main()
