import plistlib
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

from desk_controller.core.usb_observation import (
    USBPresenceObservation,
    USBSentinelConfig,
    parse_usb_observation_topic,
)
from desk_controller.desktop_agent.agent import DesktopAgent
from desk_controller.desktop_agent.usb_presence import USBPresenceProbe
from desk_controller.pi_controller.main import DeskControllerApp


class USBPresenceProbeTests(unittest.TestCase):
    def setUp(self):
        self.config = USBSentinelConfig(
            enabled=True,
            vendor_id=0x046D,
            product_id=0x0825,
            serial_number="CAM123",
        )

    @patch("desk_controller.desktop_agent.usb_presence.subprocess.run")
    def test_macos_probe_matches_structured_ioreg_output(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=plistlib.dumps(
                [
                    {
                        "IORegistryEntryChildren": [
                            {
                                "idVendor": 0x046D,
                                "idProduct": 0x0825,
                                "USB Serial Number": "CAM123",
                            }
                        ]
                    }
                ]
            ),
            stderr=b"",
        )

        self.assertTrue(USBPresenceProbe("darwin").is_present(self.config))

    @patch("desk_controller.desktop_agent.usb_presence.subprocess.run")
    def test_windows_probe_matches_present_pnp_instance(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="USB\\VID_046D&PID_0825\\CAM123\n",
            stderr="",
        )

        self.assertTrue(USBPresenceProbe("windows").is_present(self.config))

    @patch(
        "desk_controller.desktop_agent.usb_presence.subprocess.run",
        side_effect=OSError("command unavailable"),
    )
    def test_probe_failure_is_unknown_not_absent(self, _run):
        self.assertIsNone(USBPresenceProbe("darwin").is_present(self.config))


class DesktopUSBObservationTests(unittest.TestCase):
    def test_agent_publishes_retained_sentinel_presence(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation-b"
        agent._usb_observation_lock = threading.RLock()
        agent._usb_sentinel_config = USBSentinelConfig(
            enabled=True,
            vendor_id=0x046D,
            product_id=0x0825,
            poll_interval=2,
        )
        agent._usb_presence_probe = Mock()
        agent._usb_presence_probe.is_present.return_value = True
        agent._last_usb_observation = 0.0
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True

        self.assertTrue(agent._publish_usb_observation(force=True))

        topic, payload = agent.mqtt.publish.call_args.args
        self.assertEqual(topic, "desk/workstation-b/kvm/usb-observation")
        observation = USBPresenceObservation.model_validate(payload)
        self.assertTrue(observation.available)
        self.assertTrue(observation.present)
        self.assertTrue(agent.mqtt.publish.call_args.kwargs["retain"])

    def test_observation_topic_parser_rejects_unrelated_topics(self):
        self.assertEqual(
            parse_usb_observation_topic("desk/workstation-b/kvm/usb-observation"),
            "workstation-b",
        )
        self.assertIsNone(parse_usb_observation_topic("desk/workstation-b/telemetry"))


class VerifiedUGREENSwitchTests(unittest.TestCase):
    def make_controller(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.config = {
            "usb_switch": {
                "driver": "ugreen_cm691_gpio",
                "observation_enabled": True,
                "sentinel_vendor_id": 0x046D,
                "sentinel_product_id": 0x0825,
                "observation_poll_interval": 2,
                "observation_timeout": 3,
            },
            "workstations": {"pc1": "desktop", "pc2": "workstation-b"},
        }
        controller.current_pc = 0
        controller._usb_observation_condition = threading.Condition(threading.RLock())
        controller._usb_observations = {}
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {"agent_online": True}
        controller.acroname = Mock()
        controller.acroname.DRIVER = "ugreen_cm691_gpio"
        controller.acroname.active_channel = 0
        controller.mqtt = Mock()
        return controller

    @staticmethod
    def observation(device_id, present):
        return USBPresenceObservation(
            device_id=device_id,
            vendor_id=0x046D,
            product_id=0x0825,
            present=present,
        ).model_dump_json()

    def test_target_is_committed_only_after_target_agent_observes_sentinel(self):
        controller = self.make_controller()
        self.assertTrue(
            controller._ingest_usb_observation(
                "desktop",
                self.observation("desktop", True),
            )
        )
        self.assertTrue(
            controller._ingest_usb_observation(
                "workstation-b",
                self.observation("workstation-b", False),
            )
        )

        def toggle(_target):
            controller._ingest_usb_observation(
                "desktop",
                self.observation("desktop", False),
            )
            controller._ingest_usb_observation(
                "workstation-b",
                self.observation("workstation-b", True),
            )
            return True

        controller.acroname.switch_upstream_channel.side_effect = toggle

        self.assertTrue(controller._kvm_usb_set("pi", 1, uuid4()))
        controller.acroname.switch_upstream_channel.assert_called_once_with(1)
        self.assertEqual(controller._observed_usb_channel(), 1)

    def test_unknown_observation_refuses_to_guess(self):
        controller = self.make_controller()
        controller._wait_for_observed_usb_channel = Mock(return_value=None)

        self.assertFalse(controller._kvm_usb_set("pi", 1, uuid4()))
        controller.acroname.switch_upstream_channel.assert_not_called()


if __name__ == "__main__":
    unittest.main()
