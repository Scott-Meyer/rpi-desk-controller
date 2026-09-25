"""Press a Home Assistant keypad button while observing its independent LED."""

import re
from typing import Any, Mapping

from desk_controller.pi_controller.integrations.homeassistant import HomeAssistantClient

_ENTITY = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")


def read_keypad_led(button: Mapping[str, Any], ha: HomeAssistantClient) -> str:
    """Return on, off or unavailable; never infer an LED from a past press."""
    entity = str(button.get("state_entity", ""))
    if not _ENTITY.fullmatch(entity) or not entity.startswith("switch."):
        return "unavailable"
    state = ha.get_state(entity).get("state")
    return state if state in {"on", "off"} else "unavailable"


def press_keypad_button(button: Mapping[str, Any], ha: HomeAssistantClient) -> bool:
    """Ask HA to tap the programmed keypad button, not its LED switch."""
    target = str(button.get("target", ""))
    if not _ENTITY.fullmatch(target) or not target.startswith("button."):
        return False
    return ha.call_service("button.press", target)
