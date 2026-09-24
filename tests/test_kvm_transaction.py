import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch
from uuid import uuid4

from desk_controller.core.kvm import (
    KVMHardwareCommand,
    KVMHardwareResult,
    KVMSwitchRequest,
)
from desk_controller.core.pending_requests import PendingWorkstationRequests
from desk_controller.core.workstation_slots import (
    WorkstationSlotCommand,
    WorkstationSlotResult,
)
from desk_controller.pi_controller.drivers.acroname_hub import AcronameHubController
from desk_controller.pi_controller.drivers.monitor_ddc import MonitorDDCController
from desk_controller.pi_controller.drivers.usb_switch import MonitorOnlyUSBController
from desk_controller.pi_controller.main import DeskControllerApp
from desk_controller.pi_controller.workstation_deck import (
    SelectedWorkstationDeck,
)


class KVMTransactionTests(unittest.TestCase):
    def make_controller(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.current_pc = 0
        controller._kvm_lock = threading.RLock()
        controller._remote_kvm_lock = threading.RLock()
        controller._remote_kvm_waiters = {}
        controller._kvm_request_ids = {}
        controller._kvm_worker = None
        controller.kvm_fault = False
        controller.active_audio_device = "Existing Device"
        controller.config = {
            "server": {"port": 8080},
            "workstations": {
                "pc1": "windows-id",
                "pc2": "mac-id",
            },
            "monitors": [
                {
                    "inputs": {
                        "pc1": "0x0f",
                        "pc2": "0x11",
                    }
                }
            ],
        }
        controller.acroname = Mock()
        controller.acroname.PORT_COUNT = 8
        controller.acroname.MAX_CURRENT_LIMIT_MA = 4095
        controller.acroname.get_hub_status.return_value = {"ports": []}
        controller.monitor = Mock()
        controller.mqtt = Mock()
        controller.mqtt.publish.return_value = True
        controller.buttons = {}
        controller.usb_ports = {port: f"USB Port {port}" for port in range(8)}
        controller._hub_telemetry_interval = 10
        controller._last_hub_telemetry = 0
        controller._lan_ip = ""
        controller.active_groups = {}
        controller._audio_state_by_host = {}
        controller._pending_workstation_requests = PendingWorkstationRequests()
        controller.ha = Mock()
        controller._update_sd_keys = Mock()
        controller._publish_streamdeck_state = Mock()
        controller._clock_display_minute = None
        return controller

    def test_physical_kvm_key_press_is_nonblocking(self):
        # Regression test: the physical key-press handler must never block
        # on the KVM transaction directly. Doing so left the button visibly
        # unresponsive and made rapid repeat presses queue up and execute
        # serially with multi-second delays (each waiting on remote_timeout).
        controller = self.make_controller()
        controller._start_kvm_switch = Mock(return_value=True)
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": True,
        }
        controller.buttons = {
            0: {
                "enabled": True,
                "action_type": "kvm_toggle",
                "group": "kvm",
            }
        }

        controller._handle_key_press(0)

        controller._start_kvm_switch.assert_called_once_with(1)

    def test_explicit_pc_keys_do_not_toggle_an_already_selected_host(self):
        controller = self.make_controller()
        controller._start_kvm_switch = Mock(return_value=True)
        controller.buttons = {
            0: {"enabled": True, "action_type": "kvm_select", "target": "pc1"},
            3: {"enabled": True, "action_type": "kvm_select", "target": "pc2"},
        }

        controller._handle_key_press(0)
        controller._start_kvm_switch.assert_not_called()
        controller._handle_key_press(3)
        controller._start_kvm_switch.assert_called_once_with(1)

    def test_repeated_kvm_key_presses_do_not_queue_up(self):
        # A switch already in flight must cause extra presses to be ignored,
        # not queued, so mashing the button doesn't pile up serial delays.
        controller = self.make_controller()
        controller._kvm_worker = threading.Thread(target=lambda: None)
        controller._kvm_worker.is_alive = Mock(return_value=True)
        controller._switch_kvm = Mock()
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": True,
        }
        controller.buttons = {
            0: {
                "enabled": True,
                "action_type": "kvm_toggle",
                "group": "kvm",
            }
        }

        controller._handle_key_press(0)
        controller._handle_key_press(0)

        controller._switch_kvm.assert_not_called()

    def test_kvm_switch_pending_flag_is_set_immediately_and_cleared_after(self):
        controller = self.make_controller()
        controller.acroname.switch_upstream_channel.return_value = True
        controller.monitor.set_input_source.return_value = True
        seen_pending_during_switch = []

        original_switch_kvm = DeskControllerApp._switch_kvm

        def spy(self, target_pc):
            seen_pending_during_switch.append(self._kvm_switch_in_flight)
            return original_switch_kvm(self, target_pc)

        with patch.object(DeskControllerApp, "_switch_kvm", spy):
            worker = controller._start_kvm_switch(1)
            self.assertTrue(worker)
            controller._kvm_worker.join(timeout=2)

        self.assertEqual(seen_pending_during_switch, [True])
        self.assertFalse(controller._kvm_switch_in_flight)

    def test_disabled_home_assistant_skips_scene_actions_and_discovery(self):
        controller = self.make_controller()
        controller.homeassistant_enabled = False
        controller.buttons = {
            5: {
                "enabled": True,
                "label": "ON",
                "group": "lighting",
                "action_type": "ha_scene",
                "target": "scene.on",
            }
        }

        controller._handle_key_press(5)
        controller.register_ha_kvm_discovery()
        controller.register_ha_streamdeck_discovery()
        controller.register_ha_usb_hub_discovery()
        controller.ha.activate_scene.assert_not_called()
        self.assertFalse(
            any(
                mqtt_call.args[0].startswith("homeassistant/")
                for mqtt_call in controller.mqtt.publish.call_args_list
            )
        )

    def test_success_commits_state_after_both_hardware_operations(self):
        controller = self.make_controller()
        controller.acroname.switch_upstream_channel.return_value = True
        controller.monitor.set_input_source.return_value = True

        self.assertTrue(controller._switch_kvm(1))

        self.assertEqual(controller.current_pc, 1)
        self.assertFalse(controller.kvm_fault)
        self.assertEqual(controller.active_audio_device, "")
        controller.acroname.switch_upstream_channel.assert_called_once_with(1)
        controller.monitor.set_input_source.assert_called_once_with("0x11")
        self.assertIn(
            call("desk/kvm/state", "PC2", retain=True),
            controller.mqtt.publish.call_args_list,
        )

    def test_kvm_switch_restores_cached_audio_state_for_new_active_host(self):
        controller = self.make_controller()
        controller.buttons = {
            1: {
                "enabled": True,
                "action_type": "audio_output",
                "target": "Windows Speakers",
                "group": "audio",
            },
            2: {
                "enabled": True,
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            },
        }
        controller.active_groups["audio"] = 1

        controller._on_mqtt_message(
            "desk/mac-id/audio/state",
            "Mac DAC",
        )

        self.assertEqual(controller.active_audio_device, "Existing Device")
        self.assertEqual(controller._audio_state_by_host["mac-id"], "Mac DAC")

        controller.acroname.switch_upstream_channel.return_value = True
        controller.monitor.set_input_source.return_value = True
        self.assertTrue(controller._switch_kvm(1))

        self.assertEqual(controller.active_audio_device, "Mac DAC")
        self.assertEqual(controller.active_groups["audio"], 2)

    def test_audio_button_routes_to_device_id_for_committed_kvm_host(self):
        controller = self.make_controller()
        controller.current_pc = 1
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": True,
        }
        controller.buttons = {
            2: {
                "enabled": True,
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            }
        }

        controller._handle_key_press(2)

        self.assertIn(
            call("desk/mac-id/audio/set", "Mac DAC", qos=1),
            controller.mqtt.publish.call_args_list,
        )
        self.assertTrue(
            controller._pending_workstation_requests.is_pending("mac-id", 2)
        )

    def test_audio_button_is_queued_when_selected_agent_is_offline(self):
        controller = self.make_controller()
        controller.current_pc = 1
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": False,
        }
        controller.buttons = {
            2: {
                "enabled": True,
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            }
        }

        controller._handle_key_press(2)

        self.assertNotIn(
            call("desk/mac-id/audio/set", "Mac DAC", qos=1),
            controller.mqtt.publish.call_args_list,
        )
        self.assertTrue(
            controller._pending_workstation_requests.is_pending("mac-id", 2)
        )

    def test_confirmed_audio_state_clears_pending_request(self):
        controller = self.make_controller()
        controller.current_pc = 1
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": True,
        }
        controller.buttons = {
            2: {
                "enabled": True,
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            }
        }

        controller._handle_key_press(2)
        controller._on_mqtt_message("desk/mac-id/audio/state", "Mac DAC")

        self.assertFalse(
            controller._pending_workstation_requests.is_pending("mac-id", 2)
        )

    def test_audio_button_renders_unavailable_for_offline_agent(self):
        controller = self.make_controller()
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": False,
        }
        controller.streamdeck = Mock()
        controller.buttons = {
            2: {
                "enabled": True,
                "label": "MAC DAC",
                "icon": "headphones",
                "accent_color": [1, 2, 3],
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            }
        }

        DeskControllerApp._update_sd_keys(controller)

        call_for_audio = controller.streamdeck.render_scene_key.call_args_list[2]
        self.assertFalse(call_for_audio.kwargs["is_active"])
        self.assertFalse(call_for_audio.kwargs["is_available"])
        self.assertFalse(call_for_audio.kwargs["is_pending"])

    def test_pending_audio_button_renders_yellow_border_state_while_offline(self):
        controller = self.make_controller()
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": False,
        }
        controller.streamdeck = Mock()
        controller.buttons = {
            2: {
                "enabled": True,
                "label": "MAC DAC",
                "icon": "headphones",
                "accent_color": [1, 2, 3],
                "action_type": "audio_output",
                "target": "Mac DAC",
                "group": "audio",
            }
        }
        controller._pending_workstation_requests.add(
            2,
            "windows-id",
            "audio_output",
            "Mac DAC",
        )

        DeskControllerApp._update_sd_keys(controller)

        call_for_audio = controller.streamdeck.render_scene_key.call_args_list[2]
        self.assertFalse(call_for_audio.kwargs["is_available"])
        self.assertTrue(call_for_audio.kwargs["is_pending"])

    def test_datetime_buttons_refresh_only_when_the_minute_changes(self):
        controller = self.make_controller()
        controller.buttons = {
            1: {
                "enabled": True,
                "action_type": "current_time",
            }
        }

        self.assertTrue(
            controller._refresh_datetime_buttons(datetime(2026, 7, 28, 9, 5))
        )
        self.assertFalse(
            controller._refresh_datetime_buttons(datetime(2026, 7, 28, 9, 5, 30))
        )
        self.assertTrue(
            controller._refresh_datetime_buttons(datetime(2026, 7, 28, 9, 6))
        )
        self.assertEqual(controller._update_sd_keys.call_count, 2)

    @patch("desk_controller.pi_controller.main.datetime")
    def test_datetime_button_uses_dedicated_display_style(self, current_datetime):
        current_datetime.now.return_value.astimezone.return_value = datetime(
            2026, 7, 28, 9, 5
        )
        controller = self.make_controller()
        controller._update_sd_keys = DeskControllerApp._update_sd_keys.__get__(
            controller
        )
        controller.streamdeck = Mock()
        controller.current_pc = 0
        controller.kvm_fault = False
        controller.buttons = {
            1: {
                "enabled": True,
                "action_type": "current_time",
                "accent_color": [0, 200, 255],
            }
        }

        controller._update_sd_keys()

        rendered_clock = controller.streamdeck.render_scene_key.call_args_list[1]
        self.assertEqual(rendered_clock.kwargs["label"], "9:05\nAM")
        self.assertEqual(rendered_clock.kwargs["display_style"], "current_time")

    def test_available_workstation_slot_routes_to_committed_host(self):
        controller = self.make_controller()
        controller.current_pc = 1
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": True,
        }
        controller.buttons = {
            8: {
                "enabled": True,
                "action_type": "workstation_slot",
                "slot_id": "primary",
                "_agent_online": True,
                "_slot_configured": True,
            }
        }

        controller._handle_key_press(8)

        command_call = next(
            mqtt_call
            for mqtt_call in controller.mqtt.publish.call_args_list
            if mqtt_call.args[0] == "desk/mac-id/deck/command"
        )
        command = WorkstationSlotCommand.model_validate(command_call.args[1])
        self.assertEqual(command.slot_id, "primary")
        self.assertIsNotNone(command.request_id)
        self.assertEqual(command_call.kwargs["qos"], 1)

        result = WorkstationSlotResult(
            request_id=command.request_id,
            slot_id=command.slot_id,
            success=True,
        )
        controller._on_mqtt_message(
            "desk/mac-id/deck/command_result",
            result.model_dump_json(),
        )
        self.assertFalse(
            controller._pending_workstation_requests.is_pending("mac-id", 8)
        )

    def test_offline_workstation_slot_is_queued_until_agent_returns(self):
        controller = self.make_controller()
        controller.workstation_deck = Mock()
        controller.workstation_deck.selected_state.return_value = {
            "agent_online": False,
        }
        controller.buttons = {
            8: {
                "enabled": True,
                "action_type": "workstation_slot",
                "slot_id": "primary",
                "_agent_online": False,
                "_slot_configured": True,
            }
        }

        controller._handle_key_press(8)

        self.assertFalse(
            any(
                mqtt_call.args[0].endswith("/deck/command")
                for mqtt_call in controller.mqtt.publish.call_args_list
            )
        )
        self.assertTrue(
            controller._pending_workstation_requests.is_pending("windows-id", 8)
        )

    def test_kvm_switch_resolves_cached_slots_for_new_host(self):
        controller = self.make_controller()
        physical_buttons = {
            8: {
                "enabled": True,
                "action_type": "workstation_slot",
                "slot_id": "primary",
                "label": "APP SLOT",
            }
        }
        controller.workstation_deck = SelectedWorkstationDeck(physical_buttons)
        controller.workstation_deck.ingest(
            "desk/mac-id/availability",
            "online",
        )
        controller.workstation_deck.ingest(
            "desk/mac-id/deck/manifest",
            (
                '{"device_id":"mac-id","slots":['
                '{"slot_id":"primary","label":"MAC APP"}]}'
            ),
        )
        controller.buttons = controller.workstation_deck.resolve("windows-id")
        controller.acroname.switch_upstream_channel.return_value = True
        controller.monitor.set_input_source.return_value = True

        self.assertTrue(controller._switch_kvm(1))

        self.assertEqual(controller.buttons[8]["label"], "MAC APP")
        self.assertTrue(controller.buttons[8]["_agent_online"])

    def test_kvm_availability_is_not_workstation_availability(self):
        controller = self.make_controller()
        controller.workstation_deck = Mock()

        controller._on_mqtt_message("desk/kvm/availability", "online")

        controller.workstation_deck.ingest.assert_not_called()

    def test_monitor_failure_commits_usb_switch_and_flags_a_warning(self):
        controller = self.make_controller()
        controller.acroname.switch_upstream_channel.side_effect = [True, True]
        controller.monitor.set_input_source.return_value = False

        self.assertFalse(controller._switch_kvm(1))

        # The USB switch (keyboard/mouse) is kept even when the monitor step
        # fails to confirm, since that commonly just means the target's
        # desktop agent isn't running (e.g. a visiting guest's laptop).
        self.assertEqual(controller.current_pc, 1)
        self.assertTrue(controller.kvm_fault)
        self.assertEqual(
            controller.acroname.switch_upstream_channel.call_args_list,
            [call(1)],
        )
        self.assertNotIn(
            call("desk/kvm/state", "PC2", retain=True),
            controller.mqtt.publish.call_args_list,
        )
        self.assertIn(
            call("desk/kvm/availability", "offline", retain=True),
            controller.mqtt.publish.call_args_list,
        )

    @patch(
        "desk_controller.pi_controller.main.resolve_config_path",
        return_value=Path("/tmp/desk-test.yaml"),
    )
    @patch("desk_controller.pi_controller.main.load_config")
    def test_lg_alt_monitor_only_does_not_require_standard_readback(self, load, _path):
        load.return_value = {
            "acroname": {"enabled": False},
            "monitors": [{"display_id": 1, "use_alt_addressing": True}],
            "workstations": {"pc1": "", "pc2": ""},
        }
        controller = DeskControllerApp()
        self.assertFalse(controller.monitor.verify_writes)
        self.assertTrue(controller.monitor.use_alt_addressing)

    def test_monitor_routed_usb_commits_only_confirmed_monitor_input(self):
        controller = self.make_controller()
        controller.acroname = MonitorOnlyUSBController()
        controller.monitor.get_input_source.return_value = 0x11
        controller.monitor.set_input_source.return_value = False

        self.assertTrue(controller._initialize_kvm_state())
        self.assertEqual(controller.current_pc, 1)
        self.assertFalse(controller.kvm_fault)
        self.assertFalse(controller._switch_kvm(0))
        self.assertEqual(controller.current_pc, 1)
        self.assertTrue(controller.kvm_fault)

    def test_monitor_routed_usb_refuses_unobserved_startup(self):
        for monitor_input in (None, 0x99):
            with self.subTest(monitor_input=monitor_input):
                controller = self.make_controller()
                controller.acroname = MonitorOnlyUSBController()
                controller.monitor.get_input_source.return_value = monitor_input

                self.assertFalse(controller._initialize_kvm_state())
                self.assertTrue(controller.kvm_fault)
                self.assertEqual(controller.current_pc, 0)

    def test_startup_routes_usb_to_host_selected_on_monitor(self):
        controller = self.make_controller()
        controller.monitor.get_input_source.return_value = 0x11
        controller.acroname.switch_upstream_channel.return_value = True

        self.assertTrue(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 1)
        self.assertFalse(controller.kvm_fault)
        controller.acroname.switch_upstream_channel.assert_called_once_with(1)
        controller.monitor.set_input_source.assert_not_called()

    def test_startup_uses_configured_fallback_when_monitor_is_unreadable(self):
        controller = self.make_controller()
        controller.current_pc = 1
        controller.monitor.get_input_source.return_value = None
        controller.acroname.switch_upstream_channel.return_value = True

        self.assertTrue(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 1)
        controller.acroname.switch_upstream_channel.assert_called_once_with(1)

    def test_startup_faults_when_usb_cannot_follow_monitor_input(self):
        controller = self.make_controller()
        controller.monitor.get_input_source.return_value = 0x11
        controller.acroname.switch_upstream_channel.return_value = False

        self.assertFalse(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 0)
        self.assertTrue(controller.kvm_fault)

    def test_startup_trusts_observed_usb_hardware_over_configured_default(self):
        # Regression test: the default_channel config is only a guess made
        # at first-time setup. A restart must never "lie" about which PC
        # actually has keyboard/mouse control when the hub can be read
        # directly - it should reflect hardware reality instead.
        controller = self.make_controller()
        controller.current_pc = 0  # configured default still says PC1
        controller.acroname.get_hub_status.return_value = {"active_upstream": 1}
        controller.monitor.get_input_source.return_value = 0x11
        controller.acroname.switch_upstream_channel.return_value = True

        self.assertTrue(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 1)
        self.assertFalse(controller.kvm_fault)

    def test_startup_trusts_observed_usb_when_monitor_is_unreadable(self):
        controller = self.make_controller()
        controller.current_pc = 0
        controller.acroname.get_hub_status.return_value = {"active_upstream": 1}
        controller.monitor.get_input_source.return_value = None
        controller.acroname.switch_upstream_channel.return_value = True

        self.assertTrue(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 1)
        self.assertFalse(controller.kvm_fault)

    def test_startup_flags_mismatch_between_usb_and_monitor_but_trusts_usb(self):
        controller = self.make_controller()
        controller.current_pc = 0
        controller.acroname.get_hub_status.return_value = {"active_upstream": 1}
        controller.monitor.get_input_source.return_value = 0x0F  # maps to PC1
        controller.acroname.switch_upstream_channel.return_value = True

        self.assertTrue(controller._initialize_kvm_state())

        self.assertEqual(controller.current_pc, 1)
        self.assertTrue(controller.kvm_fault)

    def test_usb_failure_does_not_touch_monitor_or_commit_state(self):
        controller = self.make_controller()
        controller.acroname.switch_upstream_channel.return_value = False

        self.assertFalse(controller._switch_kvm(1))

        self.assertEqual(controller.current_pc, 0)
        self.assertTrue(controller.kvm_fault)
        controller.monitor.set_input_source.assert_not_called()
        self.assertNotIn(
            call("desk/kvm/state", "PC2", retain=True),
            controller.mqtt.publish.call_args_list,
        )

    def test_active_workstation_can_control_monitor_while_pi_controls_usb(self):
        controller = self.make_controller()
        controller.config["kvm"] = {
            "monitor_controller": "active_workstation",
            "usb_controller": "pi",
        }
        controller.acroname.switch_upstream_channel.return_value = True
        remote_result = KVMHardwareResult(
            transaction_id=uuid4(),
            command_id=uuid4(),
            operation="monitor_set",
            success=True,
        )
        controller._execute_remote_kvm_step = Mock(return_value=remote_result)

        self.assertTrue(controller._switch_kvm(1))

        controller.acroname.switch_upstream_channel.assert_called_once_with(1)
        args = controller._execute_remote_kvm_step.call_args.args
        self.assertEqual(args[0], "windows-id")
        self.assertEqual(args[2:], ("monitor_set", 1))
        controller.monitor.set_input_source.assert_not_called()

    def test_fixed_agent_can_control_usb_and_keeps_it_committed_on_monitor_failure(
        self,
    ):
        controller = self.make_controller()
        controller.config["kvm"] = {
            "monitor_controller": "pi",
            "usb_controller": "pc2",
        }
        controller.monitor.set_input_source.return_value = False
        controller._execute_remote_kvm_step = Mock(
            return_value=KVMHardwareResult(
                transaction_id=uuid4(),
                command_id=uuid4(),
                operation="usb_set",
                success=True,
            ),
        )

        self.assertFalse(controller._switch_kvm(1))

        calls = controller._execute_remote_kvm_step.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], "mac-id")
        self.assertEqual(calls[0].args[2:], ("usb_set", 1))
        controller.acroname.switch_upstream_channel.assert_not_called()
        self.assertEqual(controller.current_pc, 1)
        self.assertTrue(controller.kvm_fault)

    def test_both_hardware_steps_can_be_owned_by_active_agent(self):
        controller = self.make_controller()
        controller.config["kvm"] = {
            "monitor_controller": "active_workstation",
            "usb_controller": "active_workstation",
        }
        controller._execute_remote_kvm_step = Mock(
            side_effect=[
                KVMHardwareResult(
                    transaction_id=uuid4(),
                    command_id=uuid4(),
                    operation="usb_set",
                    success=True,
                ),
                KVMHardwareResult(
                    transaction_id=uuid4(),
                    command_id=uuid4(),
                    operation="monitor_set",
                    success=True,
                ),
            ]
        )

        self.assertTrue(controller._switch_kvm(1))

        calls = controller._execute_remote_kvm_step.call_args_list
        self.assertEqual(
            [(item.args[0], item.args[2], item.args[3]) for item in calls],
            [
                ("windows-id", "usb_set", 1),
                ("windows-id", "monitor_set", 1),
            ],
        )
        controller.acroname.switch_upstream_channel.assert_not_called()
        controller.monitor.set_input_source.assert_not_called()

    def test_workstation_hotkey_request_uses_nonblocking_coordinator_path(self):
        controller = self.make_controller()
        controller._start_kvm_switch = Mock(return_value=True)
        request = KVMSwitchRequest(
            source_device_id="mac-id",
            target="toggle",
        )

        controller._on_mqtt_message(
            "desk/kvm/request",
            request.model_dump_json(),
        )

        controller._start_kvm_switch.assert_called_once_with(1)

    def test_duplicate_hotkey_request_is_ignored(self):
        controller = self.make_controller()
        controller._start_kvm_switch = Mock(return_value=True)
        request = KVMSwitchRequest(
            source_device_id="mac-id",
            target="toggle",
        ).model_dump_json()

        controller._on_mqtt_message("desk/kvm/request", request)
        controller._on_mqtt_message("desk/kvm/request", request)

        controller._start_kvm_switch.assert_called_once_with(1)

    def test_remote_step_waits_for_exact_correlated_result(self):
        controller = self.make_controller()
        transaction_id = uuid4()

        def publish(_topic, payload, **_kwargs):
            command = KVMHardwareCommand.model_validate(payload)
            wrong_result = KVMHardwareResult(
                transaction_id=uuid4(),
                command_id=command.command_id,
                operation=command.operation,
                success=True,
                input_source=0x11,
            )
            self.assertFalse(
                controller._ingest_remote_kvm_result(
                    "mac-id",
                    wrong_result.model_dump_json(),
                )
            )
            result = wrong_result.model_copy(update={"transaction_id": transaction_id})
            self.assertTrue(
                controller._ingest_remote_kvm_result(
                    "mac-id",
                    result.model_dump_json(),
                )
            )
            return True

        controller.mqtt.publish.side_effect = publish

        result = controller._execute_remote_kvm_step(
            "mac-id",
            transaction_id,
            "monitor_get",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.input_source, 0x11)
        self.assertEqual(controller._remote_kvm_waiters, {})

    @patch(
        "desk_controller.pi_controller.main.get_lan_ip",
        return_value="192.168.50.30",
    )
    @patch(
        "desk_controller.pi_controller.main.platform.node",
        return_value="desk-pi",
    )
    def test_connect_publishes_retained_controller_status(
        self,
        _platform_node,
        _get_lan_ip,
    ):
        controller = self.make_controller()
        controller.mqtt.broker = "mqtt.internal"
        controller.mqtt.port = 1883

        controller._on_mqtt_connected()

        status_calls = [
            mqtt_call
            for mqtt_call in controller.mqtt.publish.call_args_list
            if mqtt_call.args[0] == "desk/rpi_desk_controller/status"
        ]
        self.assertEqual(len(status_calls), 1)
        self.assertEqual(status_calls[0].args[1]["lan_ip"], "192.168.50.30")
        self.assertEqual(status_calls[0].args[1]["hostname"], "desk-pi")
        self.assertTrue(status_calls[0].kwargs["retain"])

    def test_every_physical_button_press_is_published_to_mqtt(self):
        controller = self.make_controller()

        controller._handle_key_press(14)

        self.assertIn(
            call(
                "desk/rpi_desk_controller/streamdeck/action",
                "key_14",
                qos=1,
            ),
            controller.mqtt.publish.call_args_list,
        )
        event_calls = [
            mqtt_call
            for mqtt_call in controller.mqtt.publish.call_args_list
            if mqtt_call.args[0] == "desk/rpi_desk_controller/streamdeck/event"
        ]
        self.assertEqual(len(event_calls), 1)
        self.assertEqual(event_calls[0].args[1]["key"], 14)
        self.assertFalse(event_calls[0].args[1]["configured"])

    def test_successful_scene_buttons_are_mutually_exclusive_by_group(self):
        controller = self.make_controller()
        controller.buttons = {
            5: {
                "enabled": True,
                "label": "ON",
                "group": "lighting",
                "action_type": "ha_scene",
                "target": "scene.on",
                "off_target": "automation.off",
            },
            6: {
                "enabled": True,
                "label": "DIM",
                "group": "lighting",
                "action_type": "ha_scene",
                "target": "scene.dim",
                "off_target": "automation.off",
            },
        }
        controller.ha.activate_scene.return_value = True
        controller.ha.trigger_automation.return_value = True

        controller._handle_key_press(5)
        self.assertEqual(controller.active_groups["lighting"], 5)

        controller._handle_key_press(6)
        self.assertEqual(controller.active_groups["lighting"], 6)

        controller._handle_key_press(6)
        self.assertNotIn("lighting", controller.active_groups)
        controller.ha.trigger_automation.assert_called_once_with("automation.off")

    def test_generic_home_assistant_service_button_calls_local_config(self):
        controller = self.make_controller()
        controller.buttons = {
            4: {
                "enabled": True,
                "label": "FAN",
                "action_type": "ha_service",
                "service": "fan.set_percentage",
                "target": "fan.office",
                "service_data": {"percentage": 60},
            }
        }
        controller.ha.call_service.return_value = True

        controller._handle_key_press(4)

        controller.ha.call_service.assert_called_once_with(
            "fan.set_percentage",
            "fan.office",
            {"percentage": 60},
        )

    def test_stateful_ha_button_keeps_other_key_presses_responsive(self):
        controller = self.make_controller()
        controller._ha_toggle_states = {}
        controller._ha_toggle_in_flight = set()
        controller._ha_toggle_pending = {}
        controller._ha_toggle_failures = set()
        controller._ha_toggle_lock = threading.RLock()
        controller._last_ha_toggle_poll = 0.0
        controller._start_kvm_switch = Mock(return_value=True)
        controller.buttons = {
            0: {"enabled": True, "action_type": "kvm_toggle", "group": "kvm"},
            1: {
                "enabled": True,
                "label": "OFFICE OPEN",
                "active_label": "OFFICE CLOSED",
                "action_type": "ha_toggle",
                "state_entity": "cover.office_1,cover.office_2",
                "active_state": "closed",
                "inactive_state": "open",
                "service": "cover.close_cover",
                "off_service": "cover.open_cover",
                "service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
                "off_service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
            },
        }
        started = threading.Event()
        unblock = threading.Event()
        finished = threading.Event()

        def slow_state(_entity):
            started.set()
            unblock.wait(2)
            return {"state": "open"}

        controller.buttons[2] = {
            **controller.buttons[1],
            "action_type": "ha_state_action",
            "service": "cover.open_cover",
            "service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
        }
        controller.ha.get_state.side_effect = slow_state
        controller.ha.call_service.return_value = True
        controller._publish_streamdeck_state.side_effect = (
            lambda: finished.set() if controller._ha_toggle_states.get(1) else None
        )
        try:
            controller._handle_key_press(1)
            self.assertTrue(started.wait(1))
            controller._handle_key_press(0)
            controller._start_kvm_switch.assert_called_once_with(1)
            controller._handle_key_press(1)
            controller._handle_key_press(
                2
            )  # Opposite request cannot overtake pending work.
            self.assertEqual(controller.ha.get_state.call_count, 1)
        finally:
            unblock.set()
        self.assertTrue(finished.wait(2))
        controller.ha.call_service.assert_called_once_with(
            "cover.close_cover", "", controller.buttons[1]["service_data"]
        )
        self.assertEqual(controller._ha_toggle_states[1], "inactive")

    def test_ha_pending_stays_until_device_reaches_requested_state(self):
        controller = self.make_controller()
        controller.homeassistant_enabled = True
        controller._ha_toggle_lock = threading.RLock()
        controller._ha_toggle_states = {1: "inactive"}
        controller._ha_toggle_failures = set()
        controller._ha_toggle_pending = {
            "climate.air_conditioner_air_conditioner": {
                "key": 1,
                "target": "active",
                "deadline": time.monotonic() + 30,
            }
        }
        controller.physical_buttons = {
            1: {
                "action_type": "ha_state_action",
                "enabled": True,
                "state_entity": "climate.air_conditioner_air_conditioner",
                "state_attribute": "fan_mode",
                "active_state": "silent",
                "service": "mqtt.publish",
                "service_data": {"topic": "sean_ac/cmd/preset", "payload": "sleep"},
            }
        }
        controller.ha.get_state.return_value = {
            "state": "cool",
            "attributes": {"fan_mode": "turbo"},
        }
        self.assertFalse(controller._refresh_ha_toggles())
        self.assertTrue(controller._ha_toggle_pending)

        controller.ha.get_state.return_value["attributes"]["fan_mode"] = "silent"
        self.assertTrue(controller._refresh_ha_toggles())
        self.assertFalse(controller._ha_toggle_pending)
        self.assertEqual(controller._ha_toggle_states[1], "active")

    def test_home_assistant_discovery_covers_every_physical_key(self):
        controller = self.make_controller()
        controller._lan_ip = "192.168.50.30"

        controller.register_ha_streamdeck_discovery()

        discovery_calls = [
            mqtt_call
            for mqtt_call in controller.mqtt.publish.call_args_list
            if mqtt_call.args[0].startswith("homeassistant/device_automation/")
        ]
        active_calls = [call for call in discovery_calls if call.args[1]]
        self.assertEqual(len(active_calls), 15)
        self.assertEqual(active_calls[14].args[1]["payload"], "key_14")
        self.assertTrue(active_calls[14].kwargs["retain"])
        # A smaller deck removes retained triggers from a previously larger one.
        self.assertEqual(len(discovery_calls) - len(active_calls), 17)

    def test_home_assistant_discovery_exposes_rich_usb_controls(self):
        controller = self.make_controller()

        controller.register_ha_usb_hub_discovery()

        topics = {
            mqtt_call.args[0] for mqtt_call in controller.mqtt.publish.call_args_list
        }
        self.assertIn(
            "homeassistant/switch/rpi_desk_controller_usb_port_2_usb3/config",
            topics,
        )
        self.assertIn(
            "homeassistant/sensor/rpi_desk_controller_usb_port_2_device/config",
            topics,
        )
        self.assertIn(
            "homeassistant/button/rpi_desk_controller_usb_port_2_reset_power/config",
            topics,
        )

    def test_usb_hub_mqtt_switch_command_checks_and_republishes_state(self):
        controller = self.make_controller()
        controller.acroname.set_port_settings.return_value = True
        controller.acroname.get_hub_status.return_value = {
            "ports": [{"index": 3, "enabled": False}]
        }

        controller._handle_usb_hub_command(
            "desk/rpi_desk_controller/usb_hub/port/3/set",
            "OFF",
        )

        controller.acroname.set_port_settings.assert_called_once_with(
            3,
            enabled=False,
        )
        self.assertIn(
            call(
                "desk/rpi_desk_controller/usb_hub/port/3/enabled",
                "OFF",
                retain=True,
            ),
            controller.mqtt.publish.call_args_list,
        )

    def test_usb_hub_action_can_power_cycle_one_port(self):
        controller = self.make_controller()
        controller.acroname.reset_port.return_value = True
        controller.acroname.get_hub_status.return_value = {
            "ports": [{"index": 4, "enabled": True}]
        }

        result = controller._handle_usb_hub_command(
            "desk/rpi_desk_controller/usb_hub/port/4/set",
            '{"action": "reset_power"}',
        )

        self.assertTrue(result["success"])
        controller.acroname.reset_port.assert_called_once_with(
            4,
            reset_type="power",
        )

    def test_usb_hub_controls_usb2_and_usb3_independently(self):
        controller = self.make_controller()
        controller.acroname.set_port_settings.return_value = True

        result = controller._control_usb_hub_port(
            1,
            {
                "usb2_data_enabled": False,
                "usb3_data_enabled": True,
            },
        )

        self.assertTrue(result["success"])
        controller.acroname.set_port_settings.assert_called_once_with(
            1,
            usb2_data_enabled=False,
            usb3_data_enabled=True,
        )


class HardwareDriverFailureTests(unittest.TestCase):
    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.BRAINSTEM_AVAILABLE",
        False,
    )
    def test_missing_brainstem_is_not_reported_as_success(self):
        controller = AcronameHubController()

        self.assertFalse(controller.connect())
        self.assertFalse(controller.switch_upstream_channel(1))
        self.assertEqual(controller.active_channel, 0)

    def test_hardware_simulation_must_be_explicit(self):
        hub = AcronameHubController(simulate=True)
        monitor = MonitorDDCController(simulate=True)

        self.assertTrue(hub.connect())
        self.assertTrue(hub.switch_upstream_channel(1))
        self.assertEqual(hub.active_channel, 1)
        self.assertTrue(monitor.set_input_source("0x11"))
        self.assertEqual(monitor.get_input_source(), 0x11)

    @patch(
        "desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run",
    )
    def test_monitor_input_read_uses_terse_ddc_value(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="VCP 60 SNC x11\n",
            stderr="",
        )
        monitor = MonitorDDCController(display_id=2)

        self.assertEqual(monitor.get_input_source(), 0x11)
        run.assert_called_once_with(
            [
                "ddcutil",
                "--display",
                "2",
                "getvcp",
                "60",
                "--terse",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch("desk_controller.pi_controller.drivers.monitor_ddc.time.sleep")
    @patch("desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run")
    def test_monitor_only_ddc_write_ack_is_not_switch_confirmation(self, run, _sleep):
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        monitor = MonitorDDCController(verify_writes=True)
        monitor.get_input_source = Mock(return_value=0x05)

        self.assertFalse(monitor.set_input_source("0x06"))
        self.assertEqual(monitor.get_input_source.call_count, 4)

    @patch("desk_controller.pi_controller.drivers.monitor_ddc.time.sleep")
    @patch("desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run")
    def test_monitor_only_rejects_unreadable_or_transient_target_echo(
        self, run, _sleep
    ):
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        monitor = MonitorDDCController(verify_writes=True)
        monitor.get_input_source = Mock(side_effect=[None, 0x06, 0x05, 0x05])

        self.assertFalse(monitor.set_input_source("0x06"))
        self.assertEqual(monitor.get_input_source.call_count, 4)

    @patch("desk_controller.pi_controller.drivers.monitor_ddc.time.sleep")
    @patch("desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run")
    def test_monitor_only_waits_for_stable_new_input(self, run, _sleep):
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        monitor = MonitorDDCController(verify_writes=True)
        monitor.get_input_source = Mock(side_effect=[0x05, 0x06, 0x06])

        self.assertTrue(monitor.set_input_source("0x06"))
        self.assertEqual(monitor.get_input_source.call_count, 3)

    def test_simulated_hub_exposes_and_controls_all_named_ports(self):
        hub = AcronameHubController(simulate=True)
        self.assertTrue(hub.connect())

        self.assertTrue(
            hub.set_port_settings(
                2,
                enabled=False,
                usb2_data_enabled=False,
                usb3_data_enabled=True,
                current_limit_ma=1500,
            )
        )
        status = hub.get_hub_status({2: "Camera"})

        self.assertEqual(len(status["ports"]), 8)
        self.assertEqual(status["ports"][2]["name"], "Camera")
        self.assertFalse(status["ports"][2]["enabled"])
        self.assertFalse(status["ports"][2]["usb2_data_enabled"])
        self.assertTrue(status["ports"][2]["usb3_data_enabled"])
        self.assertEqual(status["ports"][2]["current_limit_a"], 1.5)
        self.assertFalse(hub.set_port_settings(8, enabled=True))
        self.assertFalse(hub.set_port_settings(2, current_limit_ma=4096))
        self.assertTrue(hub.set_name("Desk USB Hub"))
        self.assertEqual(
            hub.get_hub_status()["name"],
            "Desk USB Hub",
        )

    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.BRAINSTEM_AVAILABLE",
        True,
    )
    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.Result",
        SimpleNamespace(NO_ERROR=0),
        create=True,
    )
    def test_hardware_port_state_uses_sdk_bit_positions(self):
        def result(value):
            return SimpleNamespace(error=0, value=value)

        usb = Mock()
        usb.getPortState.return_value = result((1 << 0) | (1 << 1) | (1 << 23))
        usb.getPortVoltage.return_value = result(5_100_000)
        usb.getPortCurrent.return_value = result(250_000)
        usb.getPortCurrentLimit.return_value = result(2_500_000)
        usb.getPortMode.return_value = result(2)
        usb.getDownstreamDataSpeed.return_value = result(4)
        usb.getPortError.return_value = result((1 << 0) | (1 << 4))
        stem = SimpleNamespace(
            usb=usb,
            aUSBHUB3P_USB_VBUS_ENABLED=0,
            aUSBHUB3P_USB2_DATA_ENABLED=1,
            aUSBHUB3P_USB3_DATA_ENABLED=3,
            aUSBHUB3P_DEVICE_ATTACHED=23,
            aUSBHUB3P_USB_ERROR_FLAG=19,
        )
        hub = AcronameHubController()
        hub.stem = stem
        hub.connected = True

        status = hub.get_port_status(0)

        self.assertTrue(status["enabled"])
        self.assertTrue(status["power_enabled"])
        self.assertTrue(status["usb2_data_enabled"])
        self.assertFalse(status["usb3_data_enabled"])
        self.assertEqual(
            status["errors"],
            ["overcurrent", "short_circuit"],
        )
        self.assertTrue(status["device_attached"])
        self.assertEqual(status["voltage_v"], 5.1)

    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.BRAINSTEM_AVAILABLE",
        True,
    )
    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.Result",
        SimpleNamespace(NO_ERROR=0),
        create=True,
    )
    def test_connection_targets_configured_hub_serial(self):
        module = Mock()
        module.discoverAndConnect.return_value = 0
        brainstem_module = SimpleNamespace(
            stem=SimpleNamespace(USBHub3p=Mock(return_value=module)),
            link=SimpleNamespace(Spec=SimpleNamespace(USB=1)),
        )
        with patch(
            "desk_controller.pi_controller.drivers.acroname_hub.brainstem",
            brainstem_module,
            create=True,
        ):
            hub = AcronameHubController(serial_number=0x12345678)
            self.assertTrue(hub.connect())

        module.discoverAndConnect.assert_called_once_with(
            1,
            0x12345678,
        )

    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.BRAINSTEM_AVAILABLE",
        True,
    )
    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.Result",
        SimpleNamespace(NO_ERROR=0),
        create=True,
    )
    def test_downstream_descriptors_are_grouped_by_hub_and_port(self):
        nodes = (
            SimpleNamespace(
                hub_serial_number=0x1111,
                hub_port=3,
                id_vendor=0x046D,
                id_product=0x0825,
                speed=3,
                product_name=b"Webcam\x00",
                manufacture=b"Logitech\x00",
                serial_number=b"CAM123\x00",
            ),
            SimpleNamespace(
                hub_serial_number=0x2222,
                hub_port=3,
                id_vendor=1,
                id_product=2,
                speed=1,
                product_name="Other hub device",
                manufacture="Other",
                serial_number="OTHER",
            ),
        )
        brainstem_module = SimpleNamespace(
            discover=SimpleNamespace(
                getDownstreamDevices=Mock(
                    return_value=SimpleNamespace(error=0, value=nodes)
                )
            )
        )
        with patch(
            "desk_controller.pi_controller.drivers.acroname_hub.brainstem",
            brainstem_module,
            create=True,
        ):
            hub = AcronameHubController()
            hub.connected = True
            descriptors = hub._get_downstream_devices(0x1111)

        self.assertEqual(len(descriptors[3]), 1)
        self.assertEqual(descriptors[3][0]["product_name"], "Webcam")
        self.assertEqual(descriptors[3][0]["vid_pid"], "046d:0825")

    @patch(
        "desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run",
        side_effect=FileNotFoundError,
    )
    def test_missing_ddcutil_is_not_reported_as_success(self, _run):
        monitor = MonitorDDCController()

        self.assertFalse(monitor.set_input_source("0x11"))

    @patch(
        "desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run",
    )
    def test_alt_addressing_write_uses_lg_side_channel_without_noverify(self, run):
        # Confirmed on hardware: a plain write to 0xF4 at the monitor's normal
        # I2C address is silently accepted but has no effect. The write only
        # actually reaches LG's alt-input controller over the undocumented
        # DDC2AB side-channel at 0x50. Combining that flag with --noverify
        # also confirmed broken: ddcutil reports "Both --verify and
        # --noverify specified" while still exiting 0, so --noverify must
        # never be added here.
        run.return_value = SimpleNamespace(returncode=0, stdout="", stderr="")
        monitor = MonitorDDCController(display_id=1, use_alt_addressing=True)

        self.assertTrue(monitor.set_input_source("0x91"))

        run.assert_called_once_with(
            [
                "ddcutil",
                "--display",
                "1",
                "setvcp",
                "f4",
                "0x91",
                "--i2c-source-addr",
                "0x50",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch(
        "desk_controller.pi_controller.drivers.monitor_ddc.subprocess.run",
    )
    def test_alt_addressing_read_uses_lg_side_channel(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="VCP F4 CNC xff xff x00 x06\n",
            stderr="",
        )
        monitor = MonitorDDCController(display_id=1, use_alt_addressing=True)

        # This monitor's alt-feature reply is an unparseable "CNC" format,
        # so the read is expected to gracefully return None...
        self.assertIsNone(monitor.get_input_source())
        # ...but the command must still be issued over the side-channel.
        run.assert_called_once_with(
            [
                "ddcutil",
                "--display",
                "1",
                "getvcp",
                "f4",
                "--terse",
                "--i2c-source-addr",
                "0x50",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )


if __name__ == "__main__":
    unittest.main()
