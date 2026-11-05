"""Which scene key in each group is active, according to Home Assistant.

A scene key is active when what Home Assistant reports right now matches what
that scene sets. Where a scene's effect comes from:

* Home Assistant-defined scenes store their entity states; those are read
  from Home Assistant's scene configuration.
* Scenes owned by an integration (for example a shade gateway) keep their
  contents elsewhere. Home Assistant still shows what they did: after such a
  scene is activated, the covers that moved and where they stopped are
  recorded and kept, then used for matching from then on.

Until a scene's effect is known, its key falls back to history: the scene
Home Assistant activated most recently in the group, unless the group's
``off_target`` ran afterwards or every light it controls is now off.

``SceneObserver.refresh`` is the entry point; ``active_scene_keys`` is the
pure decision it makes from states and known effects.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

_SWITCHABLE = ("light.", "switch.", "fan.")
_MATCHED_ATTRIBUTES = ("brightness", "color_temp_kelvin", "current_position")
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
    """The comparable part of a Home Assistant scene definition."""
    effect = {}
    for entity_id, stored in (config.get("entities") or {}).items():
        if not isinstance(stored, Mapping) or "state" not in stored:
            continue
        expected = {"state": str(stored["state"])}
        if expected["state"] == "on":
            for attribute in _MATCHED_ATTRIBUTES:
                if stored.get(attribute) is not None:
                    expected[attribute] = stored[attribute]
        effect[entity_id] = expected
    return effect


def effect_matches(
    effect: Effect, state_of: Callable[[str], Mapping[str, Any]]
) -> Optional[bool]:
    """Whether current states match ``effect``; ``None`` if one can't be read."""
    for entity_id, expected in effect.items():
        state = state_of(entity_id)
        if not _readable(state) or state.get("state") == "unknown":
            return None
        attributes = state.get("attributes") or {}
        if "state" in expected and state.get("state") != expected["state"]:
            return False
        for attribute in _MATCHED_ATTRIBUTES:
            if attribute not in expected:
                continue
            actual = attributes.get(attribute)
            try:
                if (
                    abs(float(actual) - float(expected[attribute]))
                    > _TOLERANCE[attribute]
                ):
                    return False
            except (TypeError, ValueError):
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
    """Keeps scene effects current and decides scene keys from Home Assistant.

    ``ha`` needs ``get_state``, ``get_states`` and ``get_scene_config``.
    Effects learned for integration-owned scenes are saved to ``store_path``
    so they survive restarts.
    """

    CONFIG_REFRESH_SECONDS = 300
    SETTLE_SECONDS = 45
    LEARN_WINDOW_SECONDS = 600

    def __init__(self, ha, store_path: Path, clock=time.time):
        self._ha = ha
        self._store_path = Path(store_path)
        self._clock = clock
        self._defined: Dict[str, Effect] = {}  # from HA scene configuration
        self._integration_owned: set[str] = set()
        self._configs_read_at = float("-inf")
        self._learned = self._load()

    def effects(self) -> Dict[str, Effect]:
        learned = {scene: item["entities"] for scene, item in self._learned.items()}
        return {**learned, **self._defined}

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
        for scene in scenes:
            if scene in self._integration_owned:
                self._learn(scene, read(scene))
        return active_scene_keys(buttons, read, self.effects())

    def _read_definitions(self, scenes, read) -> None:
        complete = True
        for scene in scenes:
            scene_id = (read(scene).get("attributes") or {}).get("id")
            config = self._ha.get_scene_config(str(scene_id)) if scene_id else {}
            if config is None:
                complete = False
                continue
            effect = effect_from_scene_config(config)
            if effect:
                self._defined[scene] = effect
                self._integration_owned.discard(scene)
            else:
                self._defined.pop(scene, None)
                self._integration_owned.add(scene)
        if complete:
            self._configs_read_at = self._clock()

    def _learn(self, scene: str, scene_state: Mapping[str, Any]) -> None:
        """Record which covers a recent activation moved and where they stopped."""
        activated = _activated_at(scene_state)
        if activated is None:
            return
        known = self._learned.get(scene, {})
        if known.get("activated") == activated.isoformat():
            return
        age = self._clock() - activated.timestamp()
        if age < self.SETTLE_SECONDS or age > self.LEARN_WINDOW_SECONDS:
            return
        all_states = self._ha.get_states()
        if not all_states:
            return
        window_end = activated + timedelta(seconds=self.LEARN_WINDOW_SECONDS)
        moved = {}
        for state in all_states:
            entity_id = str(state.get("entity_id", ""))
            if not entity_id.startswith("cover."):
                continue
            if state.get("state") in {"opening", "closing"}:
                return  # still moving; look again on the next refresh
            try:
                changed = datetime.fromisoformat(str(state.get("last_changed")))
            except ValueError:
                continue
            position = (state.get("attributes") or {}).get("current_position")
            if activated <= changed <= window_end and position is not None:
                moved[entity_id] = {"current_position": position}
        if not moved and age < self.LEARN_WINDOW_SECONDS / 2:
            return  # nothing has reported moving yet
        entities = moved or known.get("entities")
        if not entities:
            return
        self._learned[scene] = {
            "activated": activated.isoformat(),
            "entities": entities,
        }
        logger.info("Learned what %s does from Home Assistant: %s", scene, entities)
        self._save()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            logger.warning("Ignoring unreadable scene effects at %s", self._store_path)
            return {}
        return {
            scene: item
            for scene, item in data.items()
            if isinstance(item, dict) and isinstance(item.get("entities"), dict)
        }

    def _save(self) -> None:
        temporary = self._store_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(self._learned, indent=2), encoding="utf-8")
            os.replace(temporary, self._store_path)
        except OSError:
            logger.exception("Could not save learned scene effects")
