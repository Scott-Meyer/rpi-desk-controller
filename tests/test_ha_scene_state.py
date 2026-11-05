"""Scene keys show what Home Assistant last activated, even after a restart.

Fixtures are the real Home Assistant states read from Scott's desk on
2026-11-05: LIGHTS MEDIUM was the latest light scene, the off automation ran
the morning before, and SHADES CLOSE is the latest bedroom shade scene.
"""

import threading
import unittest
from unittest.mock import Mock

from desk_controller.pi_controller.ha_scene_state import active_scene_keys
from desk_controller.pi_controller.main import DeskControllerApp

OFF = "automation.turn_off_scott"
BUTTONS = {
    0: {"action_type": "kvm_toggle", "group": "kvm"},
    5: {
        "action_type": "ha_scene",
        "group": "lighting",
        "target": "scene.scotts_room_lights_on",
        "off_target": OFF,
    },
    6: {
        "action_type": "ha_scene",
        "group": "lighting",
        "target": "scene.scotts_room_lights_dim",
        "off_target": OFF,
    },
    7: {
        "action_type": "ha_scene",
        "group": "lighting",
        "target": "scene.scotts_room_lights_meeting_bright",
        "off_target": OFF,
    },
    8: {
        "action_type": "ha_scene",
        "group": "lighting",
        "target": "scene.scotts_room_lights_medium",
        "off_target": OFF,
    },
    10: {
        "action_type": "ha_scene",
        "group": "shades",
        "target": "scene.1616_gateway_main_bedroom_close",
    },
    11: {
        "action_type": "ha_scene",
        "group": "shades",
        "target": "scene.1616_gateway_main_bedroom_privacy",
    },
    12: {
        "action_type": "ha_scene",
        "group": "shades",
        "target": "scene.1616_gateway_bedroom_extra_light",
    },
}


def desk_states():
    return {
        "scene.scotts_room_lights_on": {"state": "2026-11-05T00:06:06.137207+00:00"},
        "scene.scotts_room_lights_dim": {"state": "2026-11-04T04:30:11.304010+00:00"},
        "scene.scotts_room_lights_meeting_bright": {
            "state": "2026-11-05T01:47:46.347253+00:00"
        },
        "scene.scotts_room_lights_medium": {
            "state": "2026-11-05T02:25:29.271560+00:00"
        },
        OFF: {
            "state": "on",
            "attributes": {"last_triggered": "2026-11-04T06:02:14.631806+00:00"},
        },
        "scene.1616_gateway_main_bedroom_close": {
            "state": "2026-09-28T00:41:53.640730+00:00"
        },
        "scene.1616_gateway_main_bedroom_privacy": {
            "state": "2026-09-27T23:54:29.457786+00:00"
        },
        "scene.1616_gateway_bedroom_extra_light": {
            "state": "2026-09-27T23:53:11.293862+00:00"
        },
    }


class ActiveSceneKeyTests(unittest.TestCase):
    def test_latest_activated_scene_in_each_group_is_active(self):
        states = desk_states()

        self.assertEqual(
            active_scene_keys(BUTTONS, lambda e: states.get(e, {})),
            {"lighting": 8, "shades": 10},
        )

    def test_off_action_after_the_latest_scene_means_no_scene_is_active(self):
        states = desk_states()
        states[OFF]["attributes"]["last_triggered"] = "2026-11-05T03:00:00+00:00"

        self.assertEqual(
            active_scene_keys(BUTTONS, lambda e: states.get(e, {}))["lighting"],
            None,
        )

    def test_never_activated_scenes_mean_no_scene_is_active(self):
        states = desk_states()
        for entity in ("close", "privacy"):
            states[f"scene.1616_gateway_main_bedroom_{entity}"]["state"] = "unknown"
        states["scene.1616_gateway_bedroom_extra_light"]["state"] = "unknown"

        self.assertIsNone(
            active_scene_keys(BUTTONS, lambda e: states.get(e, {}))["shades"]
        )

    def test_unreachable_home_assistant_leaves_groups_untouched(self):
        self.assertEqual(active_scene_keys(BUTTONS, lambda _e: {}), {})


class SceneKeysAfterRestartTests(unittest.TestCase):
    def test_fresh_controller_lights_the_scene_home_assistant_reports(self):
        states = desk_states()
        app = DeskControllerApp.__new__(DeskControllerApp)
        app.homeassistant_enabled = True
        app.physical_buttons = BUTTONS
        app.ha = Mock()
        app.ha.get_state.side_effect = lambda e: states.get(e, {})
        app.active_groups = {}  # nothing pressed since the restart
        app._ha_toggle_states = {}
        app._ha_toggle_pending = {}
        app._ha_toggle_lock = threading.RLock()
        app._update_sd_keys = Mock()
        app._publish_streamdeck_state = Mock()

        self.assertTrue(app._refresh_ha_toggles())

        self.assertEqual(app.active_groups, {"lighting": 8, "shades": 10})
        app._update_sd_keys.assert_called()


if __name__ == "__main__":
    unittest.main()
