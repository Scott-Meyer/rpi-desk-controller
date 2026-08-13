"""Stream Deck layout defaults and backward-compatible configuration helpers."""

from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, Mapping

STREAMDECK_KEY_COUNT = 15
USB_HUB_PORT_COUNT = 8
DYNAMIC_DATETIME_ACTIONS = frozenset({"current_time", "current_date"})


DEFAULT_STREAMDECK_BUTTONS: Dict[int, Dict[str, Any]] = {
    0: {
        "enabled": True,
        "label": "HOST\nPC",
        "icon": "kvm",
        "accent_color": [0, 200, 255],
        "group": "kvm",
        "action_type": "kvm_toggle",
        "target": "",
        "off_target": "",
        "mqtt_topic": "",
        "mqtt_payload": "",
        "state_topic": "",
        "state_payload": "",
    },
}


def default_streamdeck_buttons() -> Dict[int, Dict[str, Any]]:
    """Return the generic Pi-owned starter layout."""
    return {0: deepcopy(DEFAULT_STREAMDECK_BUTTONS[0])}


def default_usb_ports() -> Dict[int, Dict[str, str]]:
    """Return editable labels for every downstream Acroname port."""
    return {port: {"name": f"USB Port {port}"} for port in range(USB_HUB_PORT_COUNT)}


def datetime_button_label(action_type: str, now: datetime) -> str:
    """Format a compact two-line label using the Pi's local date and time."""
    if action_type == "current_time":
        return f"{now.strftime('%I:%M').lstrip('0')}\n{now.strftime('%p')}"
    if action_type == "current_date":
        return f"{now.strftime('%a')}\n{now.strftime('%b')} {now.day}"
    raise ValueError(f"Unsupported date/time action: {action_type}")


def _integer_key_mapping(value: Any) -> Dict[int, Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    normalized = {}
    for raw_key, raw_value in value.items():
        try:
            key = int(raw_key)
        except (TypeError, ValueError):
            continue
        if 0 <= key < STREAMDECK_KEY_COUNT and isinstance(raw_value, Mapping):
            normalized[key] = dict(raw_value)
    return normalized


def configured_streamdeck_buttons(
    config: Mapping[str, Any],
) -> Dict[int, Dict[str, Any]]:
    """Load the current layout, migrating the earlier split configuration."""
    streamdeck = config.get("streamdeck", {})
    buttons = _integer_key_mapping(
        streamdeck.get("buttons", {}) if isinstance(streamdeck, Mapping) else {}
    )
    if buttons:
        return buttons

    legacy_audio = _integer_key_mapping(config.get("audio_devices", {}))
    legacy_keys = _integer_key_mapping(
        streamdeck.get("keys", {}) if isinstance(streamdeck, Mapping) else {}
    )
    if not legacy_audio and not legacy_keys:
        return default_streamdeck_buttons()

    migrated = default_streamdeck_buttons()
    for key, device in legacy_audio.items():
        migrated[key] = {
            **migrated.get(key, {}),
            "enabled": True,
            "label": device.get("label", device.get("name", "")),
            "icon": device.get("icon", "headphones"),
            "accent_color": device.get("accent_color", [0, 200, 255]),
            "group": "audio",
            "action_type": "audio_output",
            "target": device.get("name", ""),
        }
    for key, old_button in legacy_keys.items():
        migrated[key] = {
            **migrated.get(key, {}),
            "enabled": True,
            "label": old_button.get("label", ""),
            "icon": old_button.get("icon", "none"),
            "accent_color": old_button.get("accent_color", [100, 100, 110]),
            "group": (
                "lighting"
                if key in (5, 6, 7)
                else "shades"
                if key in (10, 11, 12)
                else old_button.get("group", "")
            ),
            "action_type": (
                "ha_scene"
                if old_button.get("type") == "ha_scene"
                else old_button.get("type", "none")
            ),
            "target": old_button.get("scene", ""),
            "off_target": old_button.get("off_automation", ""),
        }
    return migrated


def configured_usb_ports(config: Mapping[str, Any]) -> Dict[int, Dict[str, str]]:
    """Load configured port names while guaranteeing all eight ports exist."""
    ports = default_usb_ports()
    usb_hub = config.get("usb_hub", {})
    configured = _integer_key_mapping(
        usb_hub.get("ports", {}) if isinstance(usb_hub, Mapping) else {}
    )
    for index, values in configured.items():
        if index >= USB_HUB_PORT_COUNT:
            continue
        name = str(values.get("name", "")).strip()
        if name:
            ports[index]["name"] = name
    return ports
