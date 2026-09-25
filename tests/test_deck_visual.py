import unittest

from desk_controller.pi_controller.deck_visual import (
    ha_toggle_status,
    host_status,
    keypad_status,
)


class DeckVisualTests(unittest.TestCase):
    def test_host_reports_observed_source_and_next_press_not_stale_target(self):
        self.assertEqual(host_status("pc2").as_dict()["observed"], "PC 2")
        self.assertEqual(host_status("pc2").next_action, "→ PC 1")
        self.assertEqual(host_status("pi").next_action, "→ PC 1")
        self.assertEqual(host_status("unknown").phase, "unknown")
        self.assertEqual(host_status("unknown", fault=True).phase, "error")
        self.assertEqual(host_status(None, pending=True).target, "… READING")
        pending = host_status("pc2", pending=True, pending_pc=0)
        self.assertEqual((pending.observed, pending.target), ("PC 2", "… PC 1"))

    def test_ac_and_blinds_preserve_last_reliable_state_until_confirmed(self):
        ac = {
            "state_entity": "climate.air_conditioner_air_conditioner",
            "label": "COLD",
            "active_label": "SLEEP",
        }
        pending = ha_toggle_status(
            ac, "other", last_confirmed="inactive", pending="active"
        )
        self.assertEqual(
            (pending.observed, pending.target, pending.phase),
            ("COLD", "… SLEEP", "pending"),
        )
        self.assertEqual(ha_toggle_status(ac, "active").next_action, "→ COLD")
        self.assertEqual(
            ha_toggle_status(ac, "unavailable", pending="requesting").phase, "pending"
        )
        self.assertEqual(
            ha_toggle_status(ac, "unavailable", failed=True).phase, "error"
        )
        shades = {
            "state_entity": "cover.office_1,cover.office_2",
            "label": "OPEN",
            "active_label": "CLOSED",
        }
        self.assertEqual(
            ha_toggle_status(shades, "mixed").as_dict()["next_action"], "→ CLOSED"
        )
        self.assertEqual(ha_toggle_status(shades, "moving").phase, "blocked")

    def test_keypad_ack_keeps_observed_led_independent(self):
        sent = keypad_status("BT1", "on", acknowledged=True)
        self.assertEqual(
            (sent.control, sent.observed, sent.phase), ("BT1", "LED ON", "ack")
        )
        self.assertEqual(keypad_status("BT2", "off").next_action, "PRESS")
        self.assertEqual(keypad_status("BT3", "unavailable").observed, "LED ?")


if __name__ == "__main__":
    unittest.main()
