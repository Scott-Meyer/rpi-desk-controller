import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from desk_controller.config import save_config
from desk_controller.desktop_agent.agent import DesktopAgent
from desk_controller.desktop_agent.config_server import (
    ButtonConfigurationError,
    DesktopButtonConfigServer,
)
from desk_controller.desktop_agent.workstation_buttons import (
    DesktopWorkstationButtonRegistry,
)


def pi_layout():
    return {
        "rows": 3,
        "columns": 5,
        "buttons": [
            {
                "key": key,
                "configured": key < 13,
                "enabled": True,
                "label": "PI BUTTON",
                "icon": "none",
                "action_type": ("workstation_slot" if key in {13, 14} else "ha_scene"),
                **({"slot_id": str(key + 1)} if key in {13, 14} else {}),
            }
            for key in range(15)
        ],
    }


class DesktopAgentButtonLayoutTests(unittest.TestCase):
    def make_agent(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation-b"
        agent.hostname = "Workstation B"
        agent.os_type = "darwin"
        agent._layout_lock = threading.RLock()
        agent._streamdeck_layout = {
            "rows": 3,
            "columns": 5,
            "buttons": [],
        }
        agent._layout_received = False
        agent._last_slot_manifest = None
        agent._last_slot_state = None
        agent.audio_driver = None
        agent.media_driver = None
        agent.mqtt = Mock()
        agent.mqtt.is_connected = True
        agent.mqtt.connection_health.return_value = {
            "connected": True,
            "disconnected_seconds": None,
            "last_disconnect_reason": None,
            "recovery_count": 0,
        }
        agent.mqtt.publish.return_value = True
        agent.workstation_buttons = DesktopWorkstationButtonRegistry.from_config(
            {}, "darwin"
        )
        return agent

    def test_retained_pi_layout_exposes_only_computer_slots(self):
        agent = self.make_agent()

        self.assertTrue(agent._ingest_streamdeck_layout(json.dumps(pi_layout())))

        self.assertEqual(
            agent.available_workstation_slot_ids(),
            {"14", "15"},
        )
        snapshot = agent.button_configuration_snapshot()
        self.assertTrue(snapshot["layout_received"])
        self.assertEqual(snapshot["layout"]["buttons"][13]["slot_id"], "14")
        self.assertTrue(snapshot["mqtt_health"]["connected"])
        self.assertTrue(snapshot["pi_sync_pending"])

    def test_button_reconfiguration_republishes_without_restart(self):
        agent = self.make_agent()

        agent.reconfigure_workstation_buttons(
            {
                "15": {
                    "label": "MUTE",
                    "icon": "mute",
                    "action_type": "audio_mute",
                }
            }
        )

        topics = [call.args[0] for call in agent.mqtt.publish.call_args_list]
        self.assertIn("desk/workstation-b/deck/manifest", topics)
        self.assertIn("desk/workstation-b/deck/state", topics)
        self.assertEqual(
            agent.workstation_buttons.configured_slots()[0]["slot_id"],
            "15",
        )
        self.assertFalse(agent.button_configuration_snapshot()["pi_sync_pending"])

    def test_offline_button_save_remains_pending_for_reconnect(self):
        agent = self.make_agent()
        agent.mqtt.is_connected = False
        agent.mqtt.publish.return_value = False

        agent.reconfigure_workstation_buttons(
            {
                "15": {
                    "label": "MUTE",
                    "icon": "mute",
                    "action_type": "audio_mute",
                }
            }
        )

        snapshot = agent.button_configuration_snapshot()
        self.assertTrue(snapshot["pi_sync_pending"])


class DesktopButtonConfigServerTests(unittest.TestCase):
    def make_agent(self):
        agent = Mock()
        agent.os_type = "darwin"
        agent.mqtt = Mock()
        agent.mqtt.is_connected = True
        agent.mqtt.is_auth_failed = False
        agent.mqtt.auth_error = None
        agent.mqtt.connection_health.return_value = {
            "connected": True,
            "auth_failed": False,
            "auth_error": None,
            "disconnected_seconds": None,
            "last_disconnect_reason": None,
            "recovery_count": 0,
        }
        agent.available_workstation_slot_ids.return_value = {"14", "15"}
        agent.button_configuration_snapshot.return_value = {
            "device_id": "workstation-b",
            "hostname": "Workstation B",
            "mqtt_connected": True,
            "layout_received": True,
            "layout": pi_layout(),
            "slots": [],
            "desktop_kvm": {
                "hotkey": {
                    "enabled": False,
                    "shortcut": "ctrl+option+command+k",
                },
                "monitor": {
                    "enabled": False,
                    "backend": "auto",
                    "display_name": "",
                    "betterdisplay_path": (
                        "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
                    ),
                    "display_id": 1,
                    "inputs": {"pc1": "0x0f", "pc2": "0x11"},
                },
                "usb": {"enabled": False, "serial_number": None},
            },
        }
        return agent

    def test_save_preserves_mqtt_credentials_and_applies_buttons(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.yaml"
            save_config(
                {
                    "mqtt": {
                        "broker": "mqtt.example.test",
                        "username": "desk",
                        "password": "secret",  # pragma: allowlist secret
                    }
                },
                path,
            )
            agent = self.make_agent()
            server = DesktopButtonConfigServer(agent, path)

            server.update_configuration(
                {
                    "slots": [
                        {
                            "slot_id": "15",
                            "label": "MUTE",
                            "icon": "mute",
                            "accent_color": [255, 80, 80],
                            "action_type": "audio_mute",
                            "process_names": [],
                            "launch_target": "",
                        }
                    ],
                    "desktop_kvm": {
                        "hotkey": {
                            "enabled": True,
                            "shortcut": "ctrl+option+command+k",
                        },
                        "monitor": {
                            "enabled": True,
                            "backend": "betterdisplay",
                            "display_name": "Odyssey G75F",
                            "betterdisplay_path": (
                                "/Applications/BetterDisplay.app/Contents/"
                                "MacOS/BetterDisplay"
                            ),
                            "display_id": 1,
                            "inputs": {"pc1": "0x0f", "pc2": "0x11"},
                        },
                        "usb": {
                            "enabled": False,
                            "serial_number": None,
                        },
                    },
                }
            )

            saved = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["mqtt"]["password"], "secret")
            self.assertEqual(
                saved["workstation_slots"]["15"]["action_type"],
                "audio_mute",
            )
            self.assertTrue(saved["desktop_kvm"]["hotkey"]["enabled"])
            self.assertEqual(
                saved["desktop_kvm"]["monitor"]["display_name"],
                "Odyssey G75F",
            )
            agent.reconfigure_workstation_buttons.assert_called_once()
            agent.reconfigure_desktop_kvm.assert_called_once()

    def test_save_rejects_slots_not_advertised_by_pi(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = DesktopButtonConfigServer(
                self.make_agent(),
                Path(temp_dir) / "config.yaml",
            )

            with self.assertRaisesRegex(
                ButtonConfigurationError,
                "not advertised",
            ):
                server.update_configuration(
                    {
                        "slots": [
                            {
                                "slot_id": "9",
                                "label": "APP",
                                "action_type": "app",
                                "launch_target": "Slack",
                            }
                        ]
                    }
                )

    def test_http_api_requires_page_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = DesktopButtonConfigServer(
                self.make_agent(),
                Path(temp_dir) / "config.yaml",
                port=0,
            )
            server.start()
            try:
                page = urllib.request.urlopen(server.url, timeout=2).read()
                self.assertIn(b"Button editor", page)

                with self.assertRaises(urllib.error.HTTPError) as rejected:
                    urllib.request.urlopen(
                        server.url + "api/v1/buttons",
                        timeout=2,
                    )
                self.assertEqual(rejected.exception.code, 403)

                request = urllib.request.Request(
                    server.url + "api/v1/buttons",
                    headers={"X-Desk-Agent-Token": server.api_token},
                )
                body = json.loads(urllib.request.urlopen(request, timeout=2).read())
                self.assertEqual(body["device_id"], "workstation-b")

                # Test GET /api/v1/mqtt
                mqtt_req = urllib.request.Request(
                    server.url + "api/v1/mqtt",
                    headers={"X-Desk-Agent-Token": server.api_token},
                )
                mqtt_body = json.loads(urllib.request.urlopen(mqtt_req, timeout=2).read())
                self.assertIn("broker", mqtt_body)
                self.assertTrue(mqtt_body["connected"])

                # Test POST /api/v1/mqtt/test
                with patch("desk_controller.desktop_agent.config_server.test_mqtt_connection", return_value=(True, "Connected successfully!")):
                    test_req = urllib.request.Request(
                        server.url + "api/v1/mqtt/test",
                        data=json.dumps({"broker": "localhost", "port": 1883}).encode("utf-8"),
                        headers={"X-Desk-Agent-Token": server.api_token, "Content-Type": "application/json"},
                        method="POST",
                    )
                    test_res = json.loads(urllib.request.urlopen(test_req, timeout=2).read())
                    self.assertTrue(test_res["success"])
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()
