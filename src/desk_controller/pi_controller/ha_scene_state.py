"""Which scene key in each group is active, according to Home Assistant.

Home Assistant records when every scene was last activated (its state is that
timestamp), and when every automation or script last ran. So within a group of
``ha_scene`` keys, the active key is the scene activated most recently from
anywhere, unless the group's ``off_target`` ran after it. This survives
restarts and sees activations made outside the Stream Deck.

Current state still overrules history where Home Assistant exposes it: if
every switchable member of that scene (lights, switches, fans) is off now,
for example from a wall switch, no scene in the group is active, so the next
press turns the scene on instead of running the off action.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

_SWITCHABLE = ("light.", "switch.", "fan.")


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


def active_scene_keys(
    buttons: Mapping[int, Mapping[str, Any]],
    read_state: Callable[[str], Mapping[str, Any]],
) -> Dict[str, Optional[int]]:
    """Map each ``ha_scene`` group to its active key, or ``None`` for none.

    ``read_state(entity_id)`` returns the Home Assistant state object, or an
    empty mapping when it cannot be read. A group whose entities could not be
    read at all is left out, so a Home Assistant outage never clears a key.
    """
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
        latest: Optional[tuple[datetime, int, str]] = None
        for (key, _button), target in zip(members, targets):
            moment = _activated_at(state_of(target))
            if moment is not None and (latest is None or moment > latest[0]):
                latest = (moment, key, target)
        if latest is None:
            result[group] = None
            continue
        if any(
            (off_moment := _activated_at(state_of(off))) is not None
            and off_moment > latest[0]
            for off in offs
        ):
            result[group] = None
            continue
        switchable = [
            entity
            for entity in (state_of(latest[2]).get("attributes") or {}).get(
                "entity_id", []
            )
            if str(entity).startswith(_SWITCHABLE)
        ]
        if switchable:
            member_states = [state_of(entity).get("state") for entity in switchable]
            if any(state not in ("on", "off") for state in member_states):
                continue  # a member's power is not known; keep what is shown
            if all(state == "off" for state in member_states):
                result[group] = None
                continue
        result[group] = latest[1]
    return result
