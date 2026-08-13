import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from desk_controller.core.kvm import (
    KVMHardwareCommand,
    KVMHardwareResult,
    KVMSwitchRequest,
)
from desk_controller.desktop_agent.agent import DesktopAgent
from desk_controller.desktop_agent.hotkeys import parse_hotkey
from desk_controller.desktop_agent.kvm_hardware import (
    BetterDisplayMonitorController,
    DesktopKVMHardware,
    DesktopKVMSettings,
    DesktopMonitorKVMSettings,
)


class GlobalHotkeyTests(unittest.TestCase):
    def test_shortcut_parser_accepts_cross_platform_modifier_aliases(self):
        binding = parse_hotkey("Ctrl+Alt+Cmd+K")

        self.assertEqual(
            binding.modifiers,
            {"control", "option", "command"},
        )
        self.assertEqual(binding.key, "k")
        self.assertEqual(binding.windows_virtual_key, ord("K"))

    def test_shortcut_requires_modifier_and_one_supported_key(self):
        for shortcut in ("k", "ctrl+k+j", "ctrl+space"):
            with self.subTest(shortcut=shortcut):
                with self.assertRaises(ValueError):
                    parse_hotkey(shortcut)


class BetterDisplayMonitorTests(unittest.TestCase):
    def settings(self):
        return DesktopMonitorKVMSettings(
            enabled=True,
            backend="betterdisplay",
            display_name="Odyssey G75F",
            betterdisplay_path="/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay",
        )

    @patch("desk_controller.desktop_agent.kvm_hardware.Path.is_file", return_value=True)
    @patch("desk_controller.desktop_agent.kvm_hardware.subprocess.run")
    def test_monitor_read_uses_direct_input_select_ddc(self, run, _is_file):
        run.return_value.returncode = 0
        run.return_value.stdout = "17,18\n"
        run.return_value.stderr = ""
        controller = BetterDisplayMonitorController(self.settings())

        self.assertEqual(controller.get_input_source(), 17)
        self.assertEqual(
            run.call_args.args[0],
            [
                "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay",
                "get",
                "-name=Odyssey G75F",
                "-feature=ddc",
                "-vcp=inputSelect",
                "-value",
            ],
        )

    @patch("desk_controller.desktop_agent.kvm_hardware.Path.is_file", return_value=True)
    @patch("desk_controller.desktop_agent.kvm_hardware.subprocess.run")
    def test_monitor_write_converts_hex_input_to_ddc_value(self, run, _is_file):
        run.return_value.returncode = 0
        run.return_value.stdout = ""
        run.return_value.stderr = ""
        controller = BetterDisplayMonitorController(self.settings())

        self.assertTrue(controller.set_input_source("0x11"))
        self.assertEqual(
            run.call_args.args[0][-1],
            "-value=17",
        )


class DesktopDelegatedKVMTests(unittest.TestCase):
    def test_local_hardware_refuses_operations_until_enabled(self):
        hardware = DesktopKVMHardware(
            DesktopKVMSettings(),
            "darwin",
        )
        command = KVMHardwareCommand(
            transaction_id=uuid4(),
            operation="monitor_get",
        )

        success, value, detail = hardware.execute(command)

        self.assertFalse(success)
        self.assertIsNone(value)
        self.assertIn("disabled", detail)

    def make_agent(self):
        agent = object.__new__(DesktopAgent)
        agent.device_id = "mac-id"
        agent.mqtt = Mock()
        agent.mqtt.publish.return_value = True
        agent.kvm_hardware = Mock()
        agent.kvm_hardware.execute.return_value = (True, 0x11, "")
        agent._kvm_result_lock = threading.RLock()
        agent._kvm_result_cache = {}
        return agent

    def test_delegated_hardware_result_is_correlated_and_published(self):
        agent = self.make_agent()
        command = KVMHardwareCommand(
            transaction_id=uuid4(),
            operation="monitor_get",
        )

        agent._execute_kvm_hardware_command(command)

        publish = agent.mqtt.publish.call_args
        self.assertEqual(
            publish.args[0],
            "desk/mac-id/kvm/hardware/result",
        )
        result = KVMHardwareResult.model_validate(publish.args[1])
        self.assertEqual(result.command_id, command.command_id)
        self.assertEqual(result.input_source, 0x11)
        self.assertEqual(publish.kwargs["qos"], 1)

    def test_hotkey_publishes_toggle_request_instead_of_touching_hardware(self):
        agent = self.make_agent()

        self.assertTrue(agent.request_kvm_toggle())

        publish = agent.mqtt.publish.call_args
        self.assertEqual(publish.args[0], "desk/kvm/request")
        request = KVMSwitchRequest.model_validate(publish.args[1])
        self.assertEqual(request.source_device_id, "mac-id")
        self.assertEqual(request.target, "toggle")
        agent.kvm_hardware.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
