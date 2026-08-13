"""Thread-safe composition of workstation-owned slots into the physical deck."""

import json
import threading
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

from pydantic import ValidationError

from desk_controller.core.workstation_slots import (
    WorkstationSlotManifest,
    WorkstationSlotState,
    parse_workstation_topic,
)


class SelectedWorkstationDeck:
    """Cache every host and resolve one immutable selected-host deck snapshot."""

    def __init__(
        self,
        physical_buttons: Mapping[int, Mapping[str, Any]],
        key_count: int = 15,
    ):
        self._physical_buttons = {
            key: dict(button) for key, button in physical_buttons.items()
        }
        self._key_count = key_count
        self._manifests: Dict[str, WorkstationSlotManifest] = {}
        self._states: Dict[str, WorkstationSlotState] = {}
        self._availability: Dict[str, bool] = {}
        self._lock = threading.RLock()

    def ingest(self, topic: str, payload: str) -> Optional[str]:
        """Validate and cache one workstation message; return its device ID."""
        parsed = parse_workstation_topic(topic)
        if parsed is None:
            return None
        device_id, kind = parsed
        if kind == "command":
            return None

        with self._lock:
            if kind == "availability":
                normalized = payload.strip().lower()
                if normalized not in {"online", "offline"}:
                    return None
                self._availability[device_id] = normalized == "online"
                return device_id

            try:
                body = json.loads(payload)
                if kind == "manifest":
                    message = WorkstationSlotManifest.model_validate(body)
                    if message.device_id != device_id:
                        return None
                    self._manifests[device_id] = message
                else:
                    state = WorkstationSlotState.model_validate(body)
                    if state.device_id != device_id:
                        return None
                    self._states[device_id] = state
            except (json.JSONDecodeError, ValidationError):
                return None
        return device_id

    def resolve(self, device_id: str) -> Dict[int, Dict[str, Any]]:
        """Return a new complete button map for the selected workstation."""
        with self._lock:
            manifest = self._manifests.get(device_id)
            state = self._states.get(device_id)
            agent_online = self._availability.get(device_id, False)
            presentations = (
                {slot.slot_id: slot for slot in manifest.slots} if manifest else {}
            )
            active = state.active if state else {}
            resolved = deepcopy(self._physical_buttons)

        for key in range(self._key_count):
            if key not in resolved:
                resolved[key] = {
                    "enabled": True,
                    "label": "",
                    "icon": "none",
                    "accent_color": [0, 200, 255],
                    "group": "",
                    "action_type": "workstation_slot",
                    "slot_id": str(key + 1),
                    "_implicit_workstation_slot": True,
                }

        for button in resolved.values():
            if button.get("action_type") != "workstation_slot":
                continue
            slot_id = str(button.get("slot_id", "")).strip()
            presentation = presentations.get(slot_id)
            slot_configured = presentation is not None
            if presentation and agent_online:
                button.update(
                    {
                        "label": presentation.label,
                        "icon": presentation.icon,
                        "accent_color": list(presentation.accent_color),
                    }
                )
            else:
                button.update(
                    {
                        "label": "",
                        "icon": "none",
                    }
                )
            button["_workstation_id"] = device_id
            button["_slot_configured"] = slot_configured
            button["_agent_online"] = agent_online
            button["_slot_active"] = bool(
                agent_online and slot_configured and active.get(slot_id, False)
            )
        return resolved

    def selected_state(self, device_id: str) -> Dict[str, Any]:
        with self._lock:
            state = self._states.get(device_id)
            return {
                "device_id": device_id,
                "agent_online": self._availability.get(device_id, False),
                "active": dict(state.active) if state else {},
            }
