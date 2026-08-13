"""Validated MQTT contract for selected-workstation Stream Deck slots."""

import re
from typing import Dict, List, Literal, Optional, Tuple
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = 2
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_identifier(value: str, field_name: str) -> str:
    value = value.strip()
    if not value or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field_name} may only contain letters, numbers, dots, dashes, "
            "and underscores"
        )
    return value


class WorkstationSlotPresentation(BaseModel):
    """Workstation-owned presentation for one physical slot."""

    model_config = ConfigDict(extra="forbid")

    slot_id: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=128)
    icon: str = Field(default="none", max_length=64)
    accent_color: List[int] = Field(
        default_factory=lambda: [0, 200, 255],
        min_length=3,
        max_length=3,
    )

    @field_validator("slot_id")
    @classmethod
    def validate_slot_id(cls, value: str) -> str:
        return _validate_identifier(value, "slot_id")

    @field_validator("label", "icon")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("accent_color")
    @classmethod
    def validate_color(cls, value: List[int]) -> List[int]:
        if any(component < 0 or component > 255 for component in value):
            raise ValueError("accent color components must be between 0 and 255")
        return value


class WorkstationSlotManifest(BaseModel):
    """Complete retained slot presentation snapshot from one workstation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    device_id: str = Field(min_length=1, max_length=128)
    slots: List[WorkstationSlotPresentation] = Field(
        default_factory=list,
        max_length=15,
    )

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        return _validate_identifier(value, "device_id")

    @model_validator(mode="after")
    def unique_slots(self):
        slot_ids = [slot.slot_id for slot in self.slots]
        if len(slot_ids) != len(set(slot_ids)):
            raise ValueError("workstation slot IDs must be unique")
        return self


class WorkstationSlotState(BaseModel):
    """Complete retained active-state snapshot from one workstation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    device_id: str = Field(min_length=1, max_length=128)
    active: Dict[str, bool] = Field(default_factory=dict)

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        return _validate_identifier(value, "device_id")

    @field_validator("active")
    @classmethod
    def validate_slot_ids(cls, value: Dict[str, bool]) -> Dict[str, bool]:
        return {
            _validate_identifier(slot_id, "slot_id"): active
            for slot_id, active in value.items()
        }


class WorkstationSlotCommand(BaseModel):
    """Allowlisted command routed to the selected workstation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    request_id: Optional[UUID] = None
    slot_id: str = Field(min_length=1, max_length=64)
    action: Literal["activate"] = "activate"

    @field_validator("slot_id")
    @classmethod
    def validate_slot_id(cls, value: str) -> str:
        return _validate_identifier(value, "slot_id")


class WorkstationSlotResult(BaseModel):
    """Acknowledgement for an idempotent workstation slot command."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    request_id: UUID
    slot_id: str = Field(min_length=1, max_length=64)
    success: bool

    @field_validator("slot_id")
    @classmethod
    def validate_slot_id(cls, value: str) -> str:
        return _validate_identifier(value, "slot_id")


def availability_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/availability"


def slot_manifest_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/deck/manifest"


def slot_state_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/deck/state"


def slot_command_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/deck/command"


def slot_result_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/deck/command_result"


def parse_slot_result_topic(topic: str) -> Optional[str]:
    parts = topic.split("/")
    if (
        len(parts) == 4
        and parts[0] == "desk"
        and parts[2:] == ["deck", "command_result"]
    ):
        try:
            return _validate_identifier(parts[1], "device_id")
        except ValueError:
            return None
    return None


def parse_workstation_topic(topic: str) -> Optional[Tuple[str, str]]:
    """Return ``(device_id, message_kind)`` for an exact workstation topic."""
    parts = topic.split("/")
    if len(parts) == 3 and parts[0] == "desk" and parts[2] == "availability":
        try:
            return _validate_identifier(parts[1], "device_id"), "availability"
        except ValueError:
            return None
    if (
        len(parts) == 4
        and parts[0] == "desk"
        and parts[2] == "deck"
        and parts[3] in {"manifest", "state", "command"}
    ):
        try:
            return _validate_identifier(parts[1], "device_id"), parts[3]
        except ValueError:
            return None
    return None
