"""State-observed two-way Home Assistant controls for Stream Deck buttons."""

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from desk_controller.pi_controller.integrations.homeassistant import HomeAssistantClient


def _same_state_value(actual: Any, expected: Any) -> bool:
    """Match an integer/float sensor value without changing text semantics."""
    if isinstance(actual, bool) or isinstance(expected, bool):
        return actual == expected
    if isinstance(actual, (int, float)) or isinstance(expected, (int, float)):
        try:
            return Decimal(str(actual)) == Decimal(str(expected))
        except InvalidOperation:
            return False
    return str(actual) == str(expected)


def read_toggle_state(button: Mapping[str, Any], ha: HomeAssistantClient) -> str:
    """Return active, inactive, mixed, moving, other, or unavailable.

    A multi-cover control is active only once *every* cover reaches the target.
    Unavailable or moving entities never cause a speculative action.
    """
    entities = [
        value.strip() for value in str(button.get("state_entity", "")).split(",")
    ]
    if not entities or not all(entities) or not button.get("active_state"):
        return "unavailable"
    if not (
        action_targets_observed(button)
        if button.get("action_type") == "ha_state_action"
        else toggle_targets_observed(button)
    ):
        return "unavailable"

    states = []
    active_matches = []
    inactive_matches = []
    for entity in entities:
        result = ha.get_state(entity)
        if not isinstance(result, dict) or result.get("state") in (
            None,
            "unknown",
            "unavailable",
        ):
            return "unavailable"
        if result["state"] in ("opening", "closing"):
            return "moving"
        attribute = str(button.get("state_attribute", "")).strip()
        value = (
            result.get("attributes", {}).get(attribute)
            if attribute
            else result["state"]
        )
        if value is None:
            return "unavailable"
        states.append(value)
        active_requirements = button.get("state_requirements", {})
        inactive_requirements = button.get("inactive_requirements", {})
        if not isinstance(active_requirements, dict) or not isinstance(
            inactive_requirements, dict
        ):
            return "unavailable"

        def matches(requirements):
            return all(
                _same_state_value(
                    result["state"]
                    if key == "state"
                    else result.get("attributes", {}).get(key),
                    expected,
                )
                for key, expected in requirements.items()
            )

        active_matches.append(
            _same_state_value(value, button["active_state"])
            and matches(active_requirements)
        )
        inactive = button.get("inactive_state", "")
        inactive_matches.append(
            inactive != ""
            and _same_state_value(value, inactive)
            and matches(inactive_requirements)
        )

    if all(active_matches):
        return "active"
    if all(inactive_matches):
        return "inactive"
    if len({str(value) for value in states}) > 1:
        return "mixed"
    return "other" if inactive else "inactive"


def action_targets_observed(button: Mapping[str, Any], prefix: str = "") -> bool:
    """Reject an HA action that might reach more than the observed entities."""
    observed = {
        entity.strip() for entity in str(button.get("state_entity", "")).split(",")
    }
    if not observed or "" in observed:
        return False
    service = str(button.get(f"{prefix}service", ""))
    data = button.get(f"{prefix}service_data", {})
    if not isinstance(data, dict):
        return False
    if service == "mqtt.publish":
        return bool(data.get("topic") and data.get("payload"))
    if any(
        selector in data
        for selector in ("area_id", "device_id", "floor_id", "label_id", "target")
    ):
        return False
    # call_service uses setdefault: explicit null/empty entity_id must fail.
    raw_targets = data["entity_id"] if "entity_id" in data else button.get("target", "")
    if isinstance(raw_targets, str):
        targets = {value.strip() for value in raw_targets.split(",")}
    elif isinstance(raw_targets, list) and all(
        isinstance(value, str) for value in raw_targets
    ):
        targets = set(raw_targets)
    else:
        return False
    return targets == observed


def toggle_targets_observed(button: Mapping[str, Any]) -> bool:
    return action_targets_observed(button) and action_targets_observed(button, "off_")


def press_state_action(
    button: Mapping[str, Any], ha: HomeAssistantClient
) -> tuple[bool, str]:
    """Request one state, without reversing it on a repeated press."""
    state = read_toggle_state(button, ha)
    if state in ("unavailable", "moving"):
        return False, state
    if state == "active":
        return True, state
    success = ha.call_service(
        str(button.get("service", "")),
        str(button.get("target", "")),
        button.get("service_data", {}),
    )
    return success, state


def press_toggle(
    button: Mapping[str, Any], ha: HomeAssistantClient
) -> tuple[bool, str]:
    """Choose from freshly read state, then call the opposite configured service."""
    if not toggle_targets_observed(button):
        return False, "unavailable"
    state = read_toggle_state(button, ha)
    if state in ("unavailable", "moving"):
        return False, state
    service_prefix = "off_" if state == "active" else ""
    success = ha.call_service(
        str(button.get(f"{service_prefix}service", "")),
        str(button.get("target", "")),
        button.get(f"{service_prefix}service_data", {}),
    )
    return success, state
