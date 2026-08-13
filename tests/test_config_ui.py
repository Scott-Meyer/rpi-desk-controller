import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import yaml
from fastapi import HTTPException, Request

from desk_controller.config import load_config
from desk_controller.pi_controller.api.config_ui import (
    PiConfigurationUpdate,
    StreamDeckButtonSettings,
    _require_lan_request,
    _require_same_origin,
    configuration_page,
    configuration_script,
    configuration_styles,
    configure_config_ui,
    get_configuration,
    get_connection_status,
    restart_controller,
    update_configuration,
)


class PiConfigurationUITests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.config_path = Path(self.temp_dir.name) / "config.yaml"
        self.config_path.write_text(
            """
server:
  host: 0.0.0.0
  port: 8080
homeassistant:
  enabled: true
  url: http://homeassistant.local:8123
  token: existing-ha-token
mqtt:
  broker: homeassistant.local
  port: 1883
  username: desk
  password: existing-mqtt-password
hardware:
  simulate: false
acroname:
  serial_number: 4660
  default_channel: 0
monitors:
  - display_id: 1
    inputs:
      pc1: "0x0f"
      pc2: "0x11"
streamdeck:
  brightness: 70
  keys:
    5:
      type: ha_scene
workstations:
  pc1: workstation-a
  pc2: workstation-b
audio_devices: {}
""".strip(),
            encoding="utf-8",
        )
        self.restart = Mock()
        configure_config_ui(self.config_path, self.restart)

    def test_page_and_public_config_never_return_stored_secrets(self):
        page = configuration_page()
        page_text = Path(page.path).read_text(encoding="utf-8")
        self.assertIn("Desk Controller Setup", page_text)
        self.assertIn('href="?tab=connections"', page_text)
        self.assertIn('href="?tab=hardware"', page_text)
        self.assertIn('href="?tab=controls"', page_text)
        script_text = Path(configuration_script().path).read_text(encoding="utf-8")
        self.assertIn('searchParams.get("tab")', script_text)
        self.assertIn('"popstate"', script_text)
        self.assertIn('"pushState"', script_text)
        self.assertEqual(page.headers["cache-control"], "no-store")
        self.assertEqual(
            configuration_styles().headers["cache-control"],
            "no-store",
        )
        self.assertEqual(
            configuration_script().headers["cache-control"],
            "no-store",
        )

        body = get_configuration()
        self.assertEqual(body["mqtt"]["mode"], "external")
        self.assertTrue(body["mqtt"]["password_configured"])
        self.assertTrue(body["homeassistant"]["enabled"])
        self.assertTrue(body["homeassistant"]["token_configured"])
        self.assertNotIn("existing-mqtt-password", str(body))
        self.assertNotIn("existing-ha-token", str(body))
        self.assertEqual(len(body["usb_hub"]["ports"]), 8)
        self.assertEqual(body["kvm"]["monitor_controller"], "pi")
        self.assertEqual(body["usb_switch"]["driver"], "acroname")
        self.assertTrue(
            any(button["key"] == 5 for button in body["streamdeck"]["buttons"])
        )

    def test_update_preserves_blank_secrets_and_saves_complete_deck_layout(self):
        payload = {
            "server": {"host": "0.0.0.0", "port": 8090},
            "mqtt": {
                "mode": "local",
                "broker": "mqtt.internal",
                "port": 2883,
                "username": "new-user",
                "password": None,
                "clear_password": False,
            },
            "homeassistant": {
                "enabled": False,
                "url": "https://ha.internal",
                "token": None,
                "clear_token": False,
            },
            "hardware": {"simulate": True},
            "kvm": {
                "monitor_controller": "active_workstation",
                "usb_controller": "pc2",
                "remote_timeout": 15,
            },
            "usb_switch": {
                "driver": "ugreen_cm691_gpio",
                "default_channel": 1,
                "gpio_pin": 22,
                "gpio_active_high": True,
                "gpio_pulse_ms": 300,
                "observation_enabled": True,
                "sentinel_vendor_id": "0x046d",
                "sentinel_product_id": "0x0825",
                "sentinel_serial_number": "CAM123",
                "observation_poll_interval": 3,
                "observation_timeout": 9,
            },
            "acroname": {
                "serial_number": "0x00001234",
                "default_channel": 1,
            },
            "monitor": {
                "display_id": 2,
                "pc1_input": "0x0f",
                "pc2_input": "11",
            },
            "streamdeck": {
                "brightness": 55,
                "buttons": [
                    {
                        "key": 1,
                        "enabled": True,
                        "label": "Speakers",
                        "icon": "speakers",
                        "accent_color": [10, 20, 30],
                        "group": "audio",
                        "action_type": "audio_output",
                        "target": "USB DAC",
                        "off_target": "",
                        "mqtt_topic": "",
                        "mqtt_payload": "",
                        "state_topic": "",
                        "state_payload": "",
                    },
                    {
                        "key": 8,
                        "enabled": True,
                        "label": "App Slot",
                        "icon": "none",
                        "accent_color": [40, 50, 60],
                        "group": "",
                        "action_type": "workstation_slot",
                        "slot_id": "primary",
                        "target": "",
                        "off_target": "",
                        "mqtt_topic": "",
                        "mqtt_payload": "",
                        "state_topic": "",
                        "state_payload": "",
                    },
                ],
            },
            "usb_hub": {
                "telemetry_interval": 15,
                "ports": [
                    {"index": index, "name": f"Port {index}"} for index in range(8)
                ],
            },
            "workstations": {"pc1": "desktop", "pc2": "laptop"},
        }

        response = update_configuration(PiConfigurationUpdate.model_validate(payload))

        self.assertEqual(response["status"], "saved")
        config = load_config(str(self.config_path))
        self.assertEqual(config["mqtt"]["password"], "existing-mqtt-password")
        self.assertEqual(config["mqtt"]["mode"], "local")
        self.assertFalse(config["homeassistant"]["enabled"])
        self.assertEqual(
            config["homeassistant"]["token"],
            "existing-ha-token",
        )
        self.assertEqual(config["server"]["port"], 8090)
        self.assertEqual(config["acroname"]["serial_number"], 0x1234)
        self.assertEqual(
            config["usb_switch"],
            {
                "driver": "ugreen_cm691_gpio",
                "default_channel": 1,
                "gpio_pin": 22,
                "gpio_active_high": True,
                "gpio_pulse_ms": 300,
                "observation_enabled": True,
                "sentinel_vendor_id": 0x046D,
                "sentinel_product_id": 0x0825,
                "sentinel_serial_number": "CAM123",
                "observation_poll_interval": 3,
                "observation_timeout": 9,
            },
        )
        self.assertEqual(
            config["kvm"]["monitor_controller"],
            "active_workstation",
        )
        self.assertEqual(config["kvm"]["usb_controller"], "pc2")
        self.assertEqual(config["monitors"][0]["inputs"]["pc2"], "0x11")
        self.assertEqual(
            config["streamdeck"]["buttons"][1]["target"],
            "USB DAC",
        )
        self.assertEqual(
            config["streamdeck"]["buttons"][8]["slot_id"],
            "primary",
        )
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("keys", persisted["streamdeck"])
        self.assertNotIn("audio_devices", persisted)
        self.assertEqual(config["usb_hub"]["ports"][3]["name"], "Port 3")

    def test_disabled_home_assistant_does_not_require_a_url(self):
        payload = PiConfigurationUpdate.model_validate(
            {
                "server": {"host": "0.0.0.0", "port": 8080},
                "mqtt": {
                    "mode": "local",
                    "broker": "homeassistant.local",
                    "port": 1883,
                    "username": "",
                },
                "homeassistant": {"enabled": False, "url": ""},
                "hardware": {"simulate": False},
                "acroname": {
                    "serial_number": None,
                    "default_channel": 0,
                },
                "monitor": {
                    "display_id": 1,
                    "pc1_input": "0x0f",
                    "pc2_input": "0x11",
                },
                "streamdeck": {"brightness": 70, "buttons": []},
                "usb_hub": {
                    "telemetry_interval": 10,
                    "ports": [
                        {"index": index, "name": f"Port {index}"} for index in range(8)
                    ],
                },
                "workstations": {"pc1": "desktop", "pc2": "laptop"},
            }
        )

        self.assertFalse(payload.homeassistant.enabled)
        self.assertEqual(payload.homeassistant.url, "")

    def test_generic_home_assistant_service_button_is_validated(self):
        button = {
            "key": 4,
            "enabled": True,
            "label": "FAN",
            "action_type": "ha_service",
            "service": "fan.set_percentage",
            "target": "fan.office",
            "service_data": {"percentage": 60},
        }

        validated = PiConfigurationUpdate.model_validate(
            {
                "server": {"host": "0.0.0.0", "port": 8080},
                "mqtt": {
                    "mode": "local",
                    "broker": "localhost",
                    "port": 1883,
                    "username": "",
                },
                "homeassistant": {
                    "enabled": True,
                    "url": "http://homeassistant.local:8123",
                },
                "hardware": {"simulate": False},
                "acroname": {
                    "serial_number": None,
                    "default_channel": 0,
                },
                "monitor": {
                    "display_id": 1,
                    "pc1_input": "0x0f",
                    "pc2_input": "0x11",
                },
                "streamdeck": {
                    "brightness": 70,
                    "buttons": [button],
                },
                "usb_hub": {
                    "telemetry_interval": 10,
                    "ports": [
                        {"index": index, "name": f"Port {index}"} for index in range(8)
                    ],
                },
                "workstations": {"pc1": "desktop", "pc2": "laptop"},
            }
        )

        self.assertEqual(
            validated.streamdeck.buttons[0].service,
            "fan.set_percentage",
        )

    def test_current_date_and_time_buttons_are_validated(self):
        time_button = StreamDeckButtonSettings.model_validate(
            {"key": 1, "action_type": "current_time"}
        )
        date_button = StreamDeckButtonSettings.model_validate(
            {"key": 2, "action_type": "current_date"}
        )

        self.assertEqual(time_button.action_type, "current_time")
        self.assertEqual(date_button.action_type, "current_date")

    def test_restart_endpoint_uses_controller_callback(self):
        response = restart_controller()

        self.assertEqual(response["status"], "restarting")
        self.restart.assert_called_once_with()

    def test_connection_status_uses_live_provider(self):
        provider = Mock(
            return_value={
                "mqtt": {"connected": True},
                "homeassistant": {"authenticated": True},
            }
        )
        configure_config_ui(
            self.config_path,
            self.restart,
            connection_status_provider=provider,
        )

        self.assertEqual(
            get_connection_status()["homeassistant"]["authenticated"],
            True,
        )
        provider.assert_called_once_with()

    @staticmethod
    def request_from(
        client_host,
        *,
        host="192.168.50.30:8080",
        origin=None,
    ):
        headers = [(b"host", host.encode())]
        if origin:
            headers.append((b"origin", origin.encode()))
        return Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/config",
                "headers": headers,
                "client": (client_host, 55000),
                "server": ("192.168.50.30", 8080),
                "scheme": "http",
                "query_string": b"",
            }
        )

    def test_private_lan_clients_can_open_configuration(self):
        for client_host in (
            "127.0.0.1",
            "192.168.1.55",
            "10.20.30.40",
            "172.16.5.10",
            "fd00::10",
        ):
            with self.subTest(client_host=client_host):
                _require_lan_request(self.request_from(client_host))

    def test_public_clients_cannot_open_configuration(self):
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/config",
                "headers": [],
                "client": ("8.8.8.8", 55000),
                "server": ("raspberrypi", 8080),
                "scheme": "http",
                "query_string": b"",
            }
        )

        with self.assertRaises(HTTPException) as raised:
            _require_lan_request(request)

        self.assertEqual(raised.exception.status_code, 403)

    def test_lan_save_requires_same_origin(self):
        request = self.request_from(
            "192.168.1.55",
            origin="http://192.168.50.30:8080",
        )
        _require_same_origin(request)

        mismatched = self.request_from(
            "192.168.1.55",
            origin="https://example.com",
        )
        with self.assertRaises(HTTPException) as raised:
            _require_same_origin(mismatched)

        self.assertEqual(raised.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()
