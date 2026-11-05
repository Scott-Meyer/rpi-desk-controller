"""Which scene key in each group is active, according to Home Assistant.

Home Assistant records when every scene was last activated (its state is that
timestamp), and when every automation or script last ran. So within a group of
``ha_scene`` keys, the active key is the scene activated most recently from
anywhere, unless the group's ``off_target`` ran after it. This survives
restarts and sees activations made outside the Stream Deck.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional


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
        latest: Optional[tuple[datetime, int]] = None
        readable = False
        offs: set[str] = set()
        for key, button in members:
            state = state_of(str(button["target"]).strip())
            readable = readable or bool(state)
            moment = _activated_at(state)
            if moment is not None and (latest is None or moment > latest[0]):
                latest = (moment, key)
            off = str(button.get("off_target", "")).strip()
            if off:
                offs.add(off)
        if not readable:
            continue
        if latest is not None:
            for off in offs:
                off_moment = _activated_at(state_of(off))
                if off_moment is not None and off_moment > latest[0]:
                    latest = None
                    break
        result[group] = latest[1] if latest is not None else None
    return result
