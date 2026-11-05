"""Scene keys show what Home Assistant reports right now, even after a restart.

The fake Home Assistant below is built from real states and scene
definitions read from Scott's desk on 2026-11-05: the lights match LIGHTS
MEDIUM, and the bedroom shade scenes are gateway scenes Home Assistant holds
no definition for (last activated in September).
"""

import copy
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

from desk_controller.pi_controller.ha_scene_state import SceneObserver
from desk_controller.pi_controller.main import DeskControllerApp

OFF = "automation.turn_off_scott"
LOFTBED = "light.scott_s_room_bedroom_loftbed"
LOFT = "light.scott_bedroom_loft_lights"
SILENT = "light.scott_s_room_bedroom_silent_lights"
STRIP = "light.hue_play_gradient_lightstrip_2"
LIGHTS = [LOFTBED, LOFT, SILENT, STRIP]
BLACKOUT, PRIVACY_SHADE = "cover.main", "cover.front"
CLOSE = "scene.1616_gateway_main_bedroom_close"
PRIVACY = "scene.1616_gateway_main_bedroom_privacy"
EXTRA = "scene.1616_gateway_bedroom_extra_light"

BUTTONS = {
    0: {"action_type": "kvm_toggle", "group": "kvm"},
    5: {"action_type": "ha_scene", "group": "lighting", "off_target": OFF,
        "target": "scene.scotts_room_lights_on"},
    6: {"action_type": "ha_scene", "group": "lighting", "off_target": OFF,
        "target": "scene.scotts_room_lights_dim"},
    7: {"action_type": "ha_scene", "group": "lighting", "off_target": OFF,
        "target": "scene.scotts_room_lights_meeting_bright"},
    8: {"action_type": "ha_scene", "group": "lighting", "off_target": OFF,
        "target": "scene.scotts_room_lights_medium"},
    10: {"action_type": "ha_scene", "group": "shades", "target": CLOSE},
    11: {"action_type": "ha_scene", "group": "shades", "target": PRIVACY},
    12: {"action_type": "ha_scene", "group": "shades", "target": EXTRA},
}  # fmt: skip


def light_scene_definition(loftbed, loft, silent, strip):
    return {
        "entities": {
            LOFTBED: {"state": "on", "brightness": loftbed},
            LOFT: {"state": "on", "brightness": loft},
            SILENT: {"state": "on", "brightness": silent},
            STRIP: {
                "state": "on",
                "brightness": strip,
                "color_temp_kelvin": 2732,
                "effect": "off",
            },
        }
    }


def light(brightness, kelvin=None):
    attributes = {"brightness": brightness}
    if kelvin:
        attributes["color_temp_kelvin"] = kelvin
    return {"state": "on", "attributes": attributes}


def cover(position, changed="2026-10-13T00:25:31+00:00"):
    return {
        "state": "open" if position else "closed",
        "last_changed": changed,
        "attributes": {"current_position": position},
    }


class FakeHomeAssistant:
    """Home Assistant as seen from the desk on 2026-11-05 at 02:40 UTC."""

    NOW = datetime.fromisoformat("2026-11-05T02:40:00+00:00").timestamp()

    def __init__(self):
        scene = {
            "scene.scotts_room_lights_on": ("on", "2026-11-05T00:06:06+00:00"),
            "scene.scotts_room_lights_dim": ("dim", "2026-11-04T04:30:11+00:00"),
            "scene.scotts_room_lights_meeting_bright": (
                "meeting",
                "2026-11-05T01:47:46+00:00",
            ),
            "scene.scotts_room_lights_medium": ("medium", "2026-11-05T02:25:29+00:00"),
        }
        self.states = {
            entity: {"state": at, "attributes": {"id": sid, "entity_id": LIGHTS}}
            for entity, (sid, at) in scene.items()
        }
        self.states.update(
            {
                OFF: {"state": "on", "attributes": {"last_triggered": "2026-11-04T06:02:14+00:00"}},
                CLOSE: {"state": "2026-09-28T00:41:53+00:00", "attributes": {"room_name": "Main Bedroom"}},
                PRIVACY: {"state": "2026-09-27T23:54:29+00:00", "attributes": {"room_name": "Main Bedroom"}},
                EXTRA: {"state": "2026-09-27T23:53:11+00:00", "attributes": {"room_name": "Main Bedroom"}},
                LOFTBED: light(10),
                LOFT: light(10),
                SILENT: light(68),
                STRIP: light(30, 2732),
                BLACKOUT: cover(0),
                PRIVACY_SHADE: cover(0),
                "cover.living_room_14th": cover(0),
            }
        )  # fmt: skip
        self.definitions = {
            "on": light_scene_definition(255, 58, 255, 56),
            "dim": light_scene_definition(51, 2, 51, 3),
            "meeting": light_scene_definition(255, 114, 255, 255),
            "medium": light_scene_definition(11, 9, 69, 30),
        }
        self.now = self.NOW

    def get_state(self, entity_id):
        return copy.deepcopy(self.states.get(entity_id, {}))

    def get_states(self):
        return [{"entity_id": e, **copy.deepcopy(s)} for e, s in self.states.items()]

    def get_scene_config(self, scene_id):
        return copy.deepcopy(self.definitions.get(scene_id, {}))

    def activate(self, scene, at, moves):
        """Activate ``scene`` ``at`` (UTC ISO) and move covers as it does."""
        self.states[scene]["state"] = at
        for entity, position in moves.items():
            self.states[entity] = cover(position, changed=at.replace(":00+", ":20+"))


class SceneKeyTests(unittest.TestCase):
    def setUp(self):
        self.ha = FakeHomeAssistant()
        self.store = Path(tempfile.mkdtemp()) / "scene_effects.json"

    def keys(self, observer=None):
        observer = observer or SceneObserver(
            self.ha, self.store, clock=lambda: self.ha.now
        )
        return observer.refresh(BUTTONS)

    def test_the_desk_today_shows_lights_medium_and_shades_close(self):
        self.assertEqual(self.keys(), {"lighting": 8, "shades": 10})

    def test_lights_key_follows_actual_lights_not_the_last_scene_run(self):
        # MEETING BRIGHT ran most recently, but the lights are back at MEDIUM.
        self.ha.states["scene.scotts_room_lights_meeting_bright"]["state"] = (
            "2026-11-05T02:35:00+00:00"
        )

        self.assertEqual(self.keys()["lighting"], 8)

    def test_lights_adjusted_by_hand_match_no_scene(self):
        self.ha.states[SILENT] = light(200)

        self.assertIsNone(self.keys()["lighting"])

    def test_lights_switched_off_elsewhere_match_no_scene(self):
        for entity in LIGHTS:
            self.ha.states[entity] = {"state": "off", "attributes": {}}

        self.assertIsNone(self.keys()["lighting"])

    def test_shade_scene_learns_what_it_did_and_then_follows_the_shades(self):
        observer = SceneObserver(self.ha, self.store, clock=lambda: self.ha.now)
        self.ha.activate(
            PRIVACY, "2026-11-05T02:39:00+00:00", {BLACKOUT: 100, PRIVACY_SHADE: 0}
        )
        self.assertEqual(self.keys(observer)["shades"], 11)

        # Opened from the shade remote: no scene ran, but the shades moved.
        self.ha.states[PRIVACY_SHADE] = cover(100)
        self.assertIsNone(self.keys(observer)["shades"])

        # Back where PRIVACY puts them, after a restart (a fresh observer).
        self.ha.states[PRIVACY_SHADE] = cover(0)
        self.ha.now += 3600
        self.assertEqual(self.keys()["shades"], 11)

    def test_shades_still_moving_are_not_learned_yet(self):
        observer = SceneObserver(self.ha, self.store, clock=lambda: self.ha.now)
        self.ha.activate(CLOSE, "2026-11-05T02:39:00+00:00", {BLACKOUT: 40})
        self.ha.states[BLACKOUT]["state"] = "closing"

        self.keys(observer)

        self.assertNotIn(CLOSE, observer.effects())

    def test_missing_or_unavailable_newest_scene_changes_nothing(self):
        for broken in ({}, {"state": "unavailable", "attributes": {}}):
            self.ha.states["scene.scotts_room_lights_medium"] = broken
            self.assertNotIn("lighting", self.keys())

    def test_unavailable_light_changes_nothing(self):
        self.ha.states[SILENT] = {"state": "unavailable", "attributes": {}}

        self.assertNotIn("lighting", self.keys())

    def test_unreachable_home_assistant_changes_nothing(self):
        self.ha.states = {}

        self.assertEqual(self.keys(), {})


class SceneKeysAfterRestartTests(unittest.TestCase):
    def test_fresh_controller_lights_the_keys_home_assistant_reports(self):
        ha = FakeHomeAssistant()
        app = DeskControllerApp.__new__(DeskControllerApp)
        app.homeassistant_enabled = True
        app.physical_buttons = BUTTONS
        app.ha = Mock(wraps=ha)
        app._scene_observer = SceneObserver(
            ha, Path(tempfile.mkdtemp()) / "effects.json", clock=lambda: ha.now
        )
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
