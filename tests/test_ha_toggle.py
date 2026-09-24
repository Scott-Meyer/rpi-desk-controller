"""Observed Home Assistant button behavior at the controller/HA boundary."""

from unittest import TestCase
from unittest.mock import Mock

from desk_controller.pi_controller.ha_toggle import (
    press_state_action,
    press_toggle,
    read_toggle_state,
)


class HAToggleTests(TestCase):
    def test_office_pair_uses_both_actual_covers_and_refuses_missing_state(self):
        button = {
            "state_entity": "cover.office_1, cover.office_2",
            "active_state": "closed",
            "inactive_state": "open",
            "service": "cover.close_cover",
            "service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
            "off_service": "cover.open_cover",
            "off_service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
        }
        ha = Mock()
        ha.get_state.side_effect = lambda entity: {
            "state": "open" if entity.endswith("1") else "closed"
        }
        ha.call_service.return_value = True
        self.assertEqual(read_toggle_state(button, ha), "mixed")
        self.assertEqual(press_toggle(button, ha), (True, "mixed"))
        ha.call_service.assert_called_once_with(
            "cover.close_cover", "", button["service_data"]
        )

        ha.get_state.side_effect = lambda entity: {"state": "closed"}
        self.assertEqual(press_toggle(button, ha), (True, "active"))
        ha.call_service.assert_called_with(
            "cover.open_cover", "", button["off_service_data"]
        )

        ha.call_service.reset_mock()
        ha.get_state.side_effect = (
            lambda entity: {} if entity.endswith("2") else {"state": "open"}
        )
        self.assertEqual(press_toggle(button, ha), (False, "unavailable"))
        ha.call_service.assert_not_called()

        for transition in ("opening", "closing"):
            ha.get_state.side_effect = lambda entity: {"state": transition}
            self.assertEqual(press_toggle(button, ha), (False, "moving"))
            ha.call_service.assert_not_called()

    def test_unscoped_cover_service_never_moves_blinds(self):
        ha = Mock()
        button = {
            "state_entity": "cover.office_1,cover.office_2",
            "active_state": "closed",
            "service": "cover.close_cover",
            "off_service": "cover.open_cover",
        }
        self.assertEqual(press_toggle(button, ha), (False, "unavailable"))
        ha.call_service.assert_not_called()
        ha.get_state.assert_not_called()

    def test_extra_area_target_is_rejected_even_with_exact_entity_ids(self):
        ha = Mock()
        button = {
            "state_entity": "cover.office_1,cover.office_2",
            "active_state": "closed",
            "service": "cover.close_cover",
            "off_service": "cover.open_cover",
            "service_data": {
                "entity_id": ["cover.office_1", "cover.office_2"],
                "area_id": "all_of_upstairs",
            },
            "off_service_data": {"entity_id": ["cover.office_1", "cover.office_2"]},
        }
        self.assertEqual(press_toggle(button, ha), (False, "unavailable"))
        ha.call_service.assert_not_called()

    def test_explicit_empty_entity_id_cannot_fall_back_to_target(self):
        ha = Mock()
        button = {
            "state_entity": "cover.office_1",
            "active_state": "closed",
            "target": "cover.office_1",
            "service": "cover.close_cover",
            "off_service": "cover.open_cover",
            "service_data": {"entity_id": None},
        }
        self.assertEqual(press_toggle(button, ha), (False, "unavailable"))
        ha.call_service.assert_not_called()

    def test_explicit_sleep_requires_complete_climate_preset(self):
        button = {
            "action_type": "ha_state_action",
            "state_entity": "climate.air_conditioner_air_conditioner",
            "state_attribute": "fan_mode",
            "active_state": "silent",
            "state_requirements": {
                "state": "cool",
                "temperature": 70.5,
                "swing_mode": "off",
                "preset_mode": "none",
            },
            "service": "mqtt.publish",
            "service_data": {"topic": "sean_ac/cmd/preset", "payload": "sleep"},
        }
        ha = Mock()
        ha.call_service.return_value = True
        ha.get_state.return_value = {
            "state": "off",
            "attributes": {
                "fan_mode": "silent",
                "temperature": 70.5,
                "swing_mode": "off",
                "preset_mode": "none",
            },
        }
        self.assertEqual(press_state_action(button, ha), (True, "inactive"))
        ha.call_service.assert_called_once()
        ha.call_service.reset_mock()

        ha.get_state.return_value["state"] = "cool"
        ha.get_state.return_value["attributes"]["temperature"] = 68.0
        self.assertEqual(press_state_action(button, ha), (True, "inactive"))
        ha.call_service.assert_called_once()
        ha.call_service.reset_mock()

        ha.get_state.return_value["attributes"]["temperature"] = 70.5
        self.assertEqual(press_state_action(button, ha), (True, "active"))
        ha.call_service.assert_not_called()
        button["state_requirements"]["temperature"] = 70
        ha.get_state.return_value["attributes"]["temperature"] = 70.0
        self.assertEqual(read_toggle_state(button, ha), "active")

    def test_position_action_does_not_skip_partially_open_covers(self):
        button = {
            "action_type": "ha_state_action",
            "state_entity": "cover.office_1,cover.office_2",
            "state_attribute": "current_position",
            "active_state": "100",
            "service": "cover.set_cover_position",
            "service_data": {
                "entity_id": ["cover.office_1", "cover.office_2"],
                "position": 100,
            },
        }
        ha = Mock()
        ha.call_service.return_value = True
        ha.get_state.side_effect = [
            {"state": "open", "attributes": {"current_position": 20}},
            {"state": "open", "attributes": {"current_position": 100.0}},
        ]
        self.assertEqual(press_state_action(button, ha), (True, "mixed"))
        ha.call_service.assert_called_once_with(
            "cover.set_cover_position", "", button["service_data"]
        )

    def test_ac_preset_branch_follows_observed_fan_not_last_press(self):
        button = {
            "state_entity": "climate.air_conditioner_air_conditioner",
            "state_attribute": "fan_mode",
            "active_state": "silent",
            "inactive_state": "turbo",
            "service": "mqtt.publish",
            "service_data": {
                "topic": "sean_ac/cmd/preset",
                "payload": "sleep",
                "qos": 1,
                "retain": False,
            },
            "off_service": "mqtt.publish",
            "off_service_data": {
                "topic": "sean_ac/cmd/preset",
                "payload": "cold",
                "qos": 1,
                "retain": False,
            },
        }
        ha = Mock()
        ha.get_state.return_value = {
            "state": "cool",
            "attributes": {"fan_mode": "turbo"},
        }
        ha.call_service.return_value = True
        self.assertEqual(press_toggle(button, ha), (True, "inactive"))
        ha.call_service.assert_called_with("mqtt.publish", "", button["service_data"])

        ha.get_state.return_value = {
            "state": "cool",
            "attributes": {"fan_mode": "silent"},
        }
        self.assertEqual(press_toggle(button, ha), (True, "active"))
        ha.call_service.assert_called_with(
            "mqtt.publish", "", button["off_service_data"]
        )
