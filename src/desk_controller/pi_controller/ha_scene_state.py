"""Which scene key in each group is active, according to Home Assistant.

A scene key is active when what Home Assistant reports right now matches what
that scene sets. Home Assistant-defined scenes store their entity states, so
those are read from Home Assistant's scene configuration and compared with
the entities' current states.

Scenes owned by an integration (for example a PowerView shade gateway) keep
their targets outside Home Assistant; their effect is unknown. Their group
falls back to history: the scene Home Assistant activated most recently,
unless the group's ``off_target`` ran afterwards or every light it controls
is now off.

``SceneObserver.refresh`` is the entry point; ``active_scene_keys`` is the
pure decision it makes from states and known effects.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_SWITCHABLE = ("light.", "switch.", "fan.")
_TOLERANCE = {"brightness": 3, "color_temp_kelvin": 150, "current_position": 3}

Effect = Mapping[str, Mapping[str, Any]]  # entity_id -> expected values


def _activated_at(state: Mapping[str, Any]) -> Optional[datetime]:
    """When a scene was last activated, or a script/automation last ran."""
    for value in (
        state.get("state"),
        (state.get("attributes") or {}).get("last_triggered"),
    ):
        try:
            moment = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            continue
        if moment.tzinfo is not None:
            return moment
    return None


def _readable(state: Mapping[str, Any]) -> bool:
    """A real reply about an entity that exists. A scene that was never
    activated reads ``unknown``, which is a legitimate answer."""
    return bool(state) and state.get("state") != "unavailable"


def effect_from_scene_config(config: Mapping[str, Any]) -> Effect:
    """The comparable part of a Home Assistant scene definition.

    Brightness, the colour in the representation the scene saved it in, and
    the light effect are kept; everything else is snapshot metadata.
    """
    effect = {}
    for entity_id, stored in (config.get("entities") or {}).items():
        if not isinstance(stored, Mapping) or "state" not in stored:
            continue
        expected: Dict[str, Any] = {"state": str(stored["state"])}
        if stored.get("current_position") is not None:
            expected["current_position"] = stored["current_position"]
        if expected["state"] == "on":
            for attribute in ("brightness", "effect"):
                if stored.get(attribute) is not None:
                    expected[attribute] = stored[attribute]
            mode = stored.get("color_mode")
            if mode in (None, "color_temp") and stored.get("color_temp_kelvin"):
                expected["color_temp_kelvin"] = stored["color_temp_kelvin"]
            elif stored.get("xy_color") and mode in (
                None, "hs", "xy", "rgb", "rgbw", "rgbww",
            ):  # fmt: skip
                expected["xy_color"] = stored["xy_color"]
            elif mode not in (None, "brightness", "onoff", "color_temp") or any(
                stored.get(c) for c in ("hs_color", "rgb_color", "rgbw_color")
            ):
                expected["unsupported"] = mode or "colour"  # never claim it holds
        effect[entity_id] = expected
    return effect


def _within(actual: Any, expected: Any, tolerance: float) -> bool:
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def effect_matches(
    effect: Effect, state_of: Callable[[str], Mapping[str, Any]]
) -> Optional[bool]:
    """Whether current states match ``effect``; ``None`` if one can't be read."""
    for entity_id, expected in effect.items():
        state = state_of(entity_id)
        if not _readable(state) or state.get("state") == "unknown":
            return None
        attributes = state.get("attributes") or {}
        if "unsupported" in expected:
            return False
        if state.get("state") != expected["state"]:
            return False
        for attribute, tolerance in _TOLERANCE.items():
            if attribute in expected and not _within(
                attributes.get(attribute), expected[attribute], tolerance
            ):
                return False
        if "xy_color" in expected:
            actual_xy = attributes.get("xy_color") or ()
            if len(actual_xy) != 2 or not all(
                _within(a, e, 0.02) for a, e in zip(actual_xy, expected["xy_color"])
            ):
                return False
        if "effect" in expected:
            no_effect = ("off", "none", None)
            wanted, running = expected["effect"], attributes.get("effect")
            if (wanted in no_effect) != (running in no_effect) or (
                wanted not in no_effect and running != wanted
            ):
                return False
    return True


def active_scene_keys(
    buttons: Mapping[int, Mapping[str, Any]],
    read_state: Callable[[str], Mapping[str, Any]],
    effects: Optional[Mapping[str, Effect]] = None,
) -> Dict[str, Optional[int]]:
    """Map each ``ha_scene`` group to its active key, or ``None`` for none.

    ``read_state(entity_id)`` returns the Home Assistant state object, or an
    empty mapping when it cannot be read. ``effects`` maps scene entities to
    what they set. A group whose needed entities could not all be read is
    left out, so a Home Assistant outage never changes a key.
    """
    effects = effects or {}
    groups: Dict[str, list[tuple[int, Mapping[str, Any]]]] = {}
    for key, button in buttons.items():
        group = str(button.get("group", "")).strip()
        if (
            group
            and button.get("enabled", True)
            and button.get("action_type") == "ha_scene"
            and str(button.get("target", "")).strip()
        ):
            groups.setdefault(group, []).append((key, button))

    cache: Dict[str, Mapping[str, Any]] = {}

    def state_of(entity_id: str) -> Mapping[str, Any]:
        if entity_id not in cache:
            cache[entity_id] = read_state(entity_id) or {}
        return cache[entity_id]

    result: Dict[str, Optional[int]] = {}
    for group, members in groups.items():
        targets = [str(button["target"]).strip() for _key, button in members]
        offs = {
            str(button.get("off_target", "")).strip()
            for _key, button in members
            if str(button.get("off_target", "")).strip()
        }
        # Decide only from a complete picture: a missing newest scene or off
        # action would otherwise promote an older one.
        if not all(_readable(state_of(entity)) for entity in (*targets, *offs)):
            continue
        matches = {
            target: effect_matches(effects[target], state_of)
            for target in targets
            if effects.get(target)
        }
        if any(matched is None for matched in matches.values()):
            continue
        by_recency = sorted(
            zip(members, targets),
            key=lambda item: _activated_at(state_of(item[1]))
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        matching = [key for (key, _b), target in by_recency if matches.get(target)]
        if matching:
            result[group] = matching[0]
            continue
        if matches and len(matches) == len(targets):
            result[group] = None  # every effect is known and none is current
            continue
        decided = _decide_from_history(by_recency, offs, matches, state_of)
        if decided is not False:
            result[group] = decided
    return result


def _decide_from_history(by_recency, offs, matches, state_of):
    """Fallback for scenes whose effect is unknown. ``False`` = can't tell."""
    (latest_key, _button), latest = by_recency[0]
    latest_moment = _activated_at(state_of(latest))
    if latest_moment is None:
        return None
    if latest in matches:
        return None  # its effect is known and it no longer holds
    for off in offs:
        off_moment = _activated_at(state_of(off))
        if off_moment is not None and off_moment > latest_moment:
            return None
    switchable = [
        entity
        for entity in (state_of(latest).get("attributes") or {}).get("entity_id", [])
        if str(entity).startswith(_SWITCHABLE)
    ]
    if switchable:
        member_states = [state_of(entity).get("state") for entity in switchable]
        if any(state not in ("on", "off") for state in member_states):
            return False
        if all(state == "off" for state in member_states):
            return None
    return latest_key


class SceneObserver:
    """Reads scene definitions and decides scene keys from Home Assistant.

    ``ha`` needs ``get_state`` and ``get_scene_config``.
    """

    CONFIG_REFRESH_SECONDS = 300

    def __init__(self, ha, clock=time.time):
        self._ha = ha
        self._clock = clock
        self._defined: Dict[str, Effect] = {}  # from HA scene configuration
        self._configs_read_at = float("-inf")

    def effects(self) -> Dict[str, Effect]:
        return dict(self._defined)

    def refresh(
        self, buttons: Mapping[int, Mapping[str, Any]]
    ) -> Dict[str, Optional[int]]:
        """Read Home Assistant once and return each group's active key."""
        states: Dict[str, Mapping[str, Any]] = {}

        def read(entity_id: str) -> Mapping[str, Any]:
            if entity_id not in states:
                states[entity_id] = self._ha.get_state(entity_id) or {}
            return states[entity_id]

        scenes = sorted(
            {
                str(button.get("target", "")).strip()
                for button in buttons.values()
                if button.get("action_type") == "ha_scene"
                and button.get("enabled", True)
                and str(button.get("target", "")).strip().startswith("scene.")
            }
        )
        if self._clock() - self._configs_read_at >= self.CONFIG_REFRESH_SECONDS:
            self._read_definitions(scenes, read)
        return active_scene_keys(buttons, read, self.effects())

    def _read_definitions(self, scenes, read) -> None:
        """Refresh definitions; anything not answered keeps its last value."""
        complete = True
        for scene in scenes:
            state = read(scene)
            if not _readable(state):
                complete = False
                continue
            scene_id = (state.get("attributes") or {}).get("id")
            config = self._ha.get_scene_config(str(scene_id)) if scene_id else {}
            if config is None:
                complete = False
                continue
            effect = effect_from_scene_config(config)
            if effect:
                self._defined[scene] = effect
            else:
                self._defined.pop(scene, None)  # integration-owned: unknown
        if complete:
            self._configs_read_at = self._clock()
