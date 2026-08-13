"""Validated MQTT contract for workstation USB-sentinel observations."""

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from desk_controller.core.workstation_slots import _validate_identifier

USB_OBSERVATION_SCHEMA_VERSION = 1
USB_OBSERVATION_CONFIG_TOPIC = "desk/kvm/usb-observation/config"


class USBSentinelConfig(BaseModel):
    """Sentinel identity advertised by the Pi to every workstation agent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[USB_OBSERVATION_SCHEMA_VERSION] = (
        USB_OBSERVATION_SCHEMA_VERSION
    )
    enabled: bool = False
    vendor_id: Optional[int] = Field(default=None, ge=0, le=0xFFFF)
    product_id: Optional[int] = Field(default=None, ge=0, le=0xFFFF)
    serial_number: Optional[str] = Field(default=None, max_length=256)
    poll_interval: int = Field(default=2, ge=1, le=30)

    @field_validator("vendor_id", "product_id", mode="before")
    @classmethod
    def parse_usb_id(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            return int(value.strip(), 0)
        return value

    @field_validator("serial_number")
    @classmethod
    def normalize_serial(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    def is_complete(self) -> bool:
        return (
            self.enabled and self.vendor_id is not None and self.product_id is not None
        )

    def matches(self, observation: "USBPresenceObservation") -> bool:
        return (
            observation.vendor_id == self.vendor_id
            and observation.product_id == self.product_id
            and observation.serial_number == self.serial_number
        )


class USBPresenceObservation(BaseModel):
    """One workstation's observation of the configured USB sentinel."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[USB_OBSERVATION_SCHEMA_VERSION] = (
        USB_OBSERVATION_SCHEMA_VERSION
    )
    device_id: str = Field(min_length=1, max_length=128)
    vendor_id: int = Field(ge=0, le=0xFFFF)
    product_id: int = Field(ge=0, le=0xFFFF)
    serial_number: Optional[str] = Field(default=None, max_length=256)
    available: bool = True
    present: bool
    observed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        return _validate_identifier(value, "device_id")

    @field_validator("serial_number")
    @classmethod
    def normalize_serial(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("observed_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at must include a timezone")
        return value


def usb_observation_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/kvm/usb-observation"


def parse_usb_observation_topic(topic: str) -> Optional[str]:
    parts = topic.split("/")
    if (
        len(parts) == 4
        and parts[0] == "desk"
        and parts[1]
        and parts[2:] == ["kvm", "usb-observation"]
    ):
        try:
            return _validate_identifier(parts[1], "device_id")
        except ValueError:
            return None
    return None
