import threading
import time
import unittest
from unittest.mock import Mock

from desk_controller.pi_controller.ha_keypad import (
    press_keypad_button,
    read_keypad_led,
)
from desk_controller.pi_controller.main import DeskControllerApp


class KeypadButtonTests(unittest.TestCase):
    def test_press_dispatches_programmed_button_not_led_and_led_is_observed(self):
        button = {
            "target": "button.sean_office_keypad_button_1",
            "state_entity": "switch.sean_office_keypad_button_1_led",
        }
        ha = Mock()
        ha.get_state.return_value = {"state": "on"}
        ha.call_service.return_value = True

        self.assertEqual(read_keypad_led(button, ha), "on")
        self.assertTrue(press_keypad_button(button, ha))
        ha.call_service.assert_called_once_with(
            "button.press", "button.sean_office_keypad_button_1"
        )
        # LEAP can accept a press without changing its LED. This is still an
        # accepted press, not a timed-out failure or proof of a changed scene.
        self.assertEqual(read_keypad_led(button, ha), "on")

    def test_accepted_press_ack_is_visible_before_a_blocked_or_failed_led_read(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller._keypad_lock = threading.RLock()
        controller._keypad_pending = set()
        controller._keypad_failures = set()
        controller._keypad_ack_until = {}
        controller._keypad_led_states = {}
        controller._update_sd_keys = Mock()
        controller._publish_streamdeck_state = Mock()
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        controller.ha = Mock()
        controller.ha.call_service.return_value = True

        def blocked_led(_entity):
            started.set()
            release.wait(2)
            raise TimeoutError("LED entity temporarily unavailable")

        controller.ha.get_state.side_effect = blocked_led

        def record_update():
            if controller._keypad_led_states.get(1) == "unavailable":
                done.set()

        controller._update_sd_keys.side_effect = record_update
        button = {
            "target": "button.office_button_1",
            "state_entity": "switch.office_button_1_led",
        }
        controller._start_keypad_press(1, button)
        self.assertTrue(started.wait(1))
        try:
            self.assertNotIn(1, controller._keypad_pending)
            self.assertGreater(controller._keypad_ack_until[1], time.monotonic())
            self.assertNotIn(1, controller._keypad_failures)
            self.assertEqual(controller._update_sd_keys.call_count, 2)
        finally:
            release.set()
        self.assertTrue(done.wait(1))
        self.assertEqual(controller._keypad_led_states[1], "unavailable")
        self.assertNotIn(1, controller._keypad_failures)

    def test_older_led_response_cannot_overwrite_newer_observation(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller._keypad_lock = threading.RLock()
        controller._keypad_led_states = {}
        controller._keypad_led_generation = {}
        controller.ha = Mock()
        old_started, release = threading.Event(), threading.Event()
        calls = []

        def read_led(_entity):
            calls.append(1)
            if len(calls) == 1:
                old_started.set()
                release.wait(2)
                return {"state": "off"}
            return {"state": "on"}

        controller.ha.get_state.side_effect = read_led
        button = {"state_entity": "switch.office_led"}
        old = threading.Thread(target=controller._observe_keypad_led, args=(1, button))
        old.start()
        self.assertTrue(old_started.wait(1))
        self.assertTrue(controller._observe_keypad_led(1, button))
        release.set()
        old.join(timeout=2)
        self.assertEqual(controller._keypad_led_states[1], "on")

    def test_unavailable_led_and_invalid_button_fail_closed(self):
        ha = Mock()
        ha.get_state.return_value = {"state": "unavailable"}
        self.assertEqual(
            read_keypad_led({"state_entity": "switch.office_led"}, ha),
            "unavailable",
        )
        self.assertFalse(press_keypad_button({"target": "switch.office_led"}, ha))
        ha.call_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
