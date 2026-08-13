import threading
import unittest
from unittest.mock import Mock, patch

from desk_controller.core.models import AudioDevice, AudioState
from desk_controller.core.workstation_slots import (
    availability_topic,
)
from desk_controller.desktop_agent.agent import DesktopAgent


class DesktopAgentAudioTests(unittest.TestCase):
    def test_home_assistant_options_come_from_live_audio_devices(self):
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.device_id = "workstation"
        agent.os_type = "darwin"
        agent.audio_driver = Mock()
        agent.audio_driver.get_audio_state.return_value = AudioState(
            active_device="USB DAC",
            available_devices=[
                AudioDevice(id="usb-dac", name="USB DAC", is_default=True),
                AudioDevice(id="display", name="Display Audio"),
            ],
        )
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True

        agent.register_ha_discovery()

        audio_discovery = agent.mqtt.publish.call_args_list[0]
        self.assertEqual(
            audio_discovery.args[1]["options"],
            ["USB DAC", "Display Audio"],
        )

    def test_reconfiguring_mqtt_replaces_and_starts_the_live_client(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation"
        previous = Mock()
        agent.mqtt = previous
        replacement = Mock()
        replacement.start.return_value = True

        with patch(
            "desk_controller.desktop_agent.agent.MQTTClientHelper",
            return_value=replacement,
        ) as helper:
            result = agent.reconfigure_mqtt(
                {
                    "broker": "mqtt.example.test",
                    "port": 2883,
                    "username": "desk-agent",
                    "password": "test-password",  # pragma: allowlist secret
                }
            )

        self.assertTrue(result)
        previous.stop.assert_called_once_with()
        self.assertIs(agent.mqtt, replacement)
        self.assertEqual(
            replacement.subscribe.call_args_list,
            [
                unittest.mock.call("desk/workstation/audio/set"),
                unittest.mock.call(
                    "desk/workstation/deck/command",
                    qos=1,
                ),
                unittest.mock.call(
                    "desk/rpi_desk_controller/streamdeck/layout",
                    qos=1,
                ),
                unittest.mock.call(
                    "desk/workstation/kvm/hardware/command",
                    qos=1,
                ),
                unittest.mock.call(
                    "desk/kvm/usb-observation/config",
                    qos=1,
                ),
            ],
        )
        replacement.start.assert_called_once_with()
        helper.assert_called_once_with(
            client_id="desktop_agent_workstation",
            broker="mqtt.example.test",
            port=2883,
            username="desk-agent",
            password="test-password",  # pragma: allowlist secret
            on_message_callback=agent._on_mqtt_message,
            on_connect_callback=agent._on_mqtt_connected,
            on_connection_state_callback=agent._on_mqtt_connection_state,
            will_topic=availability_topic("workstation"),
            will_payload="offline",
            will_qos=1,
            will_retain=True,
        )

    @patch(
        "desk_controller.desktop_agent.agent.get_lan_ip",
        return_value="192.168.50.25",
    )
    def test_connect_publishes_retained_device_status(self, _get_lan_ip):
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.device_id = "workstation"
        agent.os_type = "windows"
        agent.mqtt = Mock()
        agent.mqtt.broker = "mqtt.internal"
        agent.mqtt.port = 1883
        agent.mqtt.publish.return_value = True
        agent.register_ha_discovery = Mock()

        agent._on_mqtt_connected()

        agent.register_ha_discovery.assert_called_once_with()
        self.assertEqual(agent.mqtt.publish.call_count, 2)
        topic, payload = agent.mqtt.publish.call_args_list[0].args
        self.assertEqual(topic, "desk/workstation/status")
        self.assertEqual(payload["lan_ip"], "192.168.50.25")
        self.assertEqual(payload["os_type"], "windows")
        self.assertTrue(agent.mqtt.publish.call_args_list[0].kwargs["retain"])
        self.assertEqual(
            agent.mqtt.publish.call_args_list[1],
            unittest.mock.call(
                "desk/workstation/availability",
                "online",
                qos=1,
                retain=True,
            ),
        )

    @patch(
        "desk_controller.desktop_agent.agent.get_lan_ip",
        return_value="192.168.50.25",
    )
    def test_connect_publishes_fresh_slot_state_before_online(self, _get_lan_ip):
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.device_id = "workstation"
        agent.os_type = "windows"
        agent._last_slot_state = None
        agent.audio_driver = None
        agent.workstation_buttons = Mock()
        agent.workstation_buttons.manifest_slots.return_value = []
        agent.workstation_buttons.active_state.return_value = {}
        agent.mqtt = Mock()
        agent.mqtt.broker = "mqtt.internal"
        agent.mqtt.port = 1883
        agent.mqtt.publish.return_value = True
        agent.register_ha_discovery = Mock()

        agent._on_mqtt_connected()

        topics = [mqtt_call.args[0] for mqtt_call in agent.mqtt.publish.call_args_list]
        self.assertEqual(
            topics,
            [
                "desk/workstation/status",
                "desk/workstation/deck/manifest",
                "desk/workstation/deck/state",
                "desk/workstation/availability",
            ],
        )
        self.assertEqual(
            agent.mqtt.publish.call_args_list[-1].args[1],
            "online",
        )

    @patch(
        "desk_controller.desktop_agent.agent.psutil.virtual_memory",
    )
    @patch(
        "desk_controller.desktop_agent.agent.psutil.cpu_percent",
        return_value=12.5,
    )
    def test_telemetry_includes_current_lan_ip(
        self,
        _cpu_percent,
        virtual_memory,
    ):
        virtual_memory.return_value.percent = 34.5
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.device_id = "workstation"
        agent.os_type = "darwin"
        agent.lan_ip = "192.168.50.25"
        agent.audio_driver = Mock()
        agent.audio_driver.get_audio_state.return_value = AudioState(
            active_device="USB DAC",
            available_devices=[],
        )
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True

        agent.publish_state()

        telemetry_call = agent.mqtt.publish.call_args_list[0]
        self.assertEqual(
            telemetry_call.args[1]["lan_ip"],
            "192.168.50.25",
        )

    def test_offline_run_loop_skips_expensive_state_polling(self):
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.os_type = "darwin"
        agent.device_id = "workstation"
        agent.mqtt = Mock()
        agent.mqtt.start.return_value = True
        agent.mqtt.wait_until_connected.return_value = False
        agent.mqtt.is_connected = False
        agent.publish_state = Mock()
        agent._stop_event = Mock()
        agent._stop_event.is_set.side_effect = [False, True]
        agent._poll_wake_event = Mock()

        agent.run()

        agent.publish_state.assert_not_called()
        agent._poll_wake_event.wait.assert_called_once_with(
            DesktopAgent.OFFLINE_POLL_INTERVAL
        )

    def test_connected_run_loop_keeps_normal_state_polling(self):
        agent = object.__new__(DesktopAgent)
        agent.hostname = "workstation"
        agent.os_type = "darwin"
        agent.device_id = "workstation"
        agent.mqtt = Mock()
        agent.mqtt.start.return_value = True
        agent.mqtt.wait_until_connected.return_value = True
        agent.mqtt.is_connected = True
        agent.publish_state = Mock()
        agent._stop_event = Mock()
        agent._stop_event.is_set.side_effect = [False, True]
        agent._poll_wake_event = Mock()

        agent.run()

        agent.publish_state.assert_called_once_with()
        agent._poll_wake_event.wait.assert_called_once_with(
            DesktopAgent.ONLINE_POLL_INTERVAL
        )

    def test_stop_publishes_graceful_offline_state(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "workstation"
        agent._stop_event = threading.Event()
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True
        agent.mqtt.stop.return_value = True

        self.assertTrue(agent.stop())

        self.assertTrue(agent._stop_event.is_set())
        agent.mqtt.publish.assert_called_once_with(
            "desk/workstation/availability",
            "offline",
            qos=1,
            retain=True,
        )
        agent.mqtt.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
