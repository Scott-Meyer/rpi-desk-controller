import unittest
from unittest.mock import Mock, patch

from desk_controller.pi_controller.drivers.acroname_hub import (
    AcronameHubController,
)
from desk_controller.pi_controller.drivers.usb_switch import (
    GPIOToggleUSBController,
    create_usb_controller,
)


class USBControllerFactoryTests(unittest.TestCase):
    def test_legacy_acroname_configuration_remains_supported(self):
        controller = create_usb_controller(
            {
                "acroname": {
                    "serial_number": 0x12345678,
                    "default_channel": 1,
                }
            },
            simulate=True,
        )

        self.assertIsInstance(controller, AcronameHubController)
        self.assertEqual(controller.serial_number, 0x12345678)

    def test_ugreen_alias_builds_gpio_toggle_controller(self):
        controller = create_usb_controller(
            {
                "usb_switch": {
                    "driver": "ugreen_cm691_gpio",
                    "default_channel": 1,
                    "gpio_pin": 22,
                    "gpio_active_high": True,
                    "gpio_pulse_ms": 350,
                }
            },
            simulate=True,
        )

        self.assertIsInstance(controller, GPIOToggleUSBController)
        self.assertEqual(controller.active_channel, 1)
        self.assertEqual(controller.gpio_pin, 22)
        self.assertTrue(controller.active_high)
        self.assertEqual(controller.pulse_ms, 350)

    def test_unknown_driver_fails_during_configuration(self):
        with self.assertRaisesRegex(ValueError, "usb_switch.driver"):
            create_usb_controller({"usb_switch": {"driver": "mystery_switch"}})


class GPIOToggleUSBControllerTests(unittest.TestCase):
    def test_switch_pulses_once_only_when_target_changes(self):
        output = Mock()
        output_factory = Mock(return_value=output)
        controller = GPIOToggleUSBController(
            17,
            active_high=False,
            pulse_ms=50,
            initial_channel=0,
            output_factory=output_factory,
        )

        self.assertTrue(controller.connect())
        with patch(
            "desk_controller.pi_controller.drivers.usb_switch.time.sleep"
        ) as sleep:
            self.assertTrue(controller.switch_upstream_channel(0))
            self.assertTrue(controller.switch_upstream_channel(1))

        output_factory.assert_called_once_with(
            17,
            active_high=False,
            initial_value=False,
        )
        output.on.assert_called_once_with()
        output.off.assert_called_once_with()
        sleep.assert_called_once_with(0.05)
        self.assertEqual(controller.active_channel, 1)

    def test_failed_pulse_does_not_commit_assumed_channel(self):
        output = Mock()
        output.on.side_effect = RuntimeError("relay failed")
        controller = GPIOToggleUSBController(
            17,
            initial_channel=0,
            output_factory=Mock(return_value=output),
        )
        self.assertTrue(controller.connect())

        self.assertFalse(controller.switch_upstream_channel(1))

        output.off.assert_called_once_with()
        self.assertEqual(controller.active_channel, 0)

    def test_status_advertises_missing_telemetry_and_port_control(self):
        controller = GPIOToggleUSBController(17, simulate=True)
        self.assertTrue(controller.connect())

        status = controller.get_hub_status({0: "Keyboard"})

        self.assertEqual(status["driver"], "ugreen_cm691_gpio")
        self.assertEqual(status["manufacturer"], "UGREEN")
        self.assertFalse(status["state_feedback"])
        self.assertFalse(status["capabilities"]["per_port_control"])
        self.assertEqual(len(status["ports"]), 4)
        self.assertEqual(status["ports"][0]["name"], "Keyboard")
        self.assertFalse(status["ports"][0]["controllable"])

    def test_gpio_and_pulse_ranges_are_validated(self):
        with self.assertRaises(ValueError):
            GPIOToggleUSBController(1)
        with self.assertRaises(ValueError):
            GPIOToggleUSBController(17, pulse_ms=10)


if __name__ == "__main__":
    unittest.main()
