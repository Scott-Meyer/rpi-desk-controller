import json
import unittest
from unittest.mock import Mock

from fastapi import HTTPException
from pydantic import ValidationError

from desk_controller.pi_controller.api.app import (
    SelectAudioPayload,
    USBHubPortCommand,
    clear_telemetry,
    configure_command_publisher,
    configure_usb_hub_api,
    control_usb_hub_port,
    get_usb_hub,
    ingest_mqtt_telemetry,
    set_active_audio_device,
    telemetry_snapshot,
)


class MQTTRestBridgeTests(unittest.TestCase):
    def setUp(self):
        clear_telemetry()
        configure_command_publisher(None)
        configure_usb_hub_api(None, None)

    def test_mqtt_telemetry_populates_rest_snapshot(self):
        payload = {
            "device_id": "workstation-a",
            "hostname": "Workstation A",
            "os_type": "windows",
            "cpu_percent": 12.5,
            "memory_percent": 44.0,
            "active_audio": "Desk Speakers",
        }

        self.assertTrue(
            ingest_mqtt_telemetry(
                "desk/workstation-a/telemetry",
                json.dumps(payload),
            )
        )

        snapshot = telemetry_snapshot()
        self.assertEqual(snapshot["workstation-a"]["active_audio"], "Desk Speakers")
        self.assertIn("last_updated", snapshot["workstation-a"])

    def test_invalid_or_mismatched_telemetry_is_rejected(self):
        self.assertFalse(ingest_mqtt_telemetry("desk/workstation-a/state", "{}"))
        self.assertFalse(
            ingest_mqtt_telemetry("desk/workstation-a/telemetry", "not json")
        )
        self.assertFalse(
            ingest_mqtt_telemetry(
                "desk/workstation-a/telemetry",
                json.dumps(
                    {
                        "device_id": "workstation-b",
                        "hostname": "Workstation B",
                        "os_type": "darwin",
                        "cpu_percent": 1,
                        "memory_percent": 2,
                    }
                ),
            )
        )
        self.assertEqual(telemetry_snapshot(), {})

    def test_rest_audio_command_publishes_to_device_topic(self):
        publisher = Mock(return_value=True)
        configure_command_publisher(publisher)

        response = set_active_audio_device(
            SelectAudioPayload(
                device_id="workstation-a",
                audio_device_name="Desk Speakers",
            )
        )

        publisher.assert_called_once_with(
            "desk/workstation-a/audio/set",
            "Desk Speakers",
        )
        self.assertEqual(response["status"], "published")

    def test_rest_audio_command_reports_mqtt_unavailable(self):
        configure_command_publisher(Mock(return_value=False))

        with self.assertRaises(HTTPException) as context:
            set_active_audio_device(
                SelectAudioPayload(
                    device_id="workstation-a",
                    audio_device_name="Desk Speakers",
                )
            )

        self.assertEqual(context.exception.status_code, 503)

    def test_device_id_cannot_inject_an_mqtt_topic(self):
        with self.assertRaises(ValidationError):
            SelectAudioPayload(
                device_id="workstation-a/audio/set",
                audio_device_name="Desk Speakers",
            )

    def test_usb_hub_rest_endpoints_use_live_controller_callbacks(self):
        status_provider = Mock(return_value={"connected": True, "ports": []})
        port_controller = Mock(return_value={"success": True, "state": {"index": 2}})
        configure_usb_hub_api(status_provider, port_controller)

        self.assertTrue(get_usb_hub()["connected"])
        result = control_usb_hub_port(
            2,
            USBHubPortCommand(
                usb2_data_enabled=False,
                usb3_data_enabled=True,
            ),
        )

        self.assertTrue(result["success"])
        port_controller.assert_called_once_with(
            2,
            {
                "usb2_data_enabled": False,
                "usb3_data_enabled": True,
            },
        )

    def test_usb_hub_action_cannot_be_combined_with_settings(self):
        with self.assertRaises(ValidationError):
            USBHubPortCommand(
                action="reset_power",
                power_enabled=True,
            )


if __name__ == "__main__":
    unittest.main()
