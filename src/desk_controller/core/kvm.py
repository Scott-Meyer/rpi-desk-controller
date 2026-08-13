"""Validated MQTT contract for coordinated local and remote KVM control."""

from typing import Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from desk_controller.core.workstation_slots import _validate_identifier

KVM_SCHEMA_VERSION = 1
KVM_REQUEST_TOPIC = "desk/kvm/request"


class KVMSwitchRequest(BaseModel):
    """A workstation request for the Pi coordinator to switch hosts."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[KVM_SCHEMA_VERSION] = KVM_SCHEMA_VERSION
    request_id: UUID = Field(default_factory=uuid4)
    source_device_id: str = Field(min_length=1, max_length=128)
    target: Literal["toggle", "pc1", "pc2"] = "toggle"

    @field_validator("source_device_id")
    @classmethod
    def validate_source_device_id(cls, value: str) -> str:
        return _validate_identifier(value, "source_device_id")


class KVMHardwareCommand(BaseModel):
    """One allowlisted hardware step delegated by the Pi to a workstation."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[KVM_SCHEMA_VERSION] = KVM_SCHEMA_VERSION
    transaction_id: UUID
    command_id: UUID = Field(default_factory=uuid4)
    operation: Literal["monitor_get", "monitor_set", "usb_set"]
    target_pc: Optional[int] = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def target_required_for_write(self):
        if self.operation != "monitor_get" and self.target_pc is None:
            raise ValueError(f"{self.operation} requires target_pc")
        if self.operation == "monitor_get" and self.target_pc is not None:
            raise ValueError("monitor_get does not accept target_pc")
        return self


class KVMHardwareResult(BaseModel):
    """A correlated result for one delegated hardware step."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[KVM_SCHEMA_VERSION] = KVM_SCHEMA_VERSION
    transaction_id: UUID
    command_id: UUID
    operation: Literal["monitor_get", "monitor_set", "usb_set"]
    success: bool
    input_source: Optional[int] = Field(default=None, ge=0, le=255)
    detail: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def monitor_read_returns_a_value(self):
        if self.success and self.operation == "monitor_get":
            if self.input_source is None:
                raise ValueError("successful monitor_get requires input_source")
        elif self.input_source is not None:
            raise ValueError("input_source is valid only for monitor_get")
        return self


def kvm_hardware_command_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/kvm/hardware/command"


def kvm_hardware_result_topic(device_id: str) -> str:
    return f"desk/{_validate_identifier(device_id, 'device_id')}/kvm/hardware/result"


def parse_kvm_hardware_result_topic(topic: str) -> Optional[str]:
    parts = topic.split("/")
    if (
        len(parts) == 5
        and parts[0] == "desk"
        and parts[1]
        and parts[2:] == ["kvm", "hardware", "result"]
    ):
        try:
            return _validate_identifier(parts[1], "device_id")
        except ValueError:
            return None
    return None
