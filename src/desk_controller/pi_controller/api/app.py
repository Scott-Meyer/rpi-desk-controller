"""Read-only telemetry API and REST-to-MQTT command bridge."""

import json
import re
import threading
import time
from copy import deepcopy
from typing import Any, Callable, Dict, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from desk_controller.core.models import TelemetryMetrics
from desk_controller.pi_controller.api.config_ui import (
    _require_lan_request,
    _require_same_origin,
)
from desk_controller.pi_controller.api.config_ui import (
    router as config_router,
)

app = FastAPI(title="Desk Controller MQTT Bridge API", version="1.1.0")
app.include_router(config_router)

_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_TELEMETRY_LOCK = threading.RLock()
_TELEMETRY_DB: Dict[str, Dict[str, Any]] = {}
_COMMAND_PUBLISHER: Optional[Callable[[str, Any], bool]] = None
_USB_HUB_STATUS_PROVIDER: Optional[Callable[[], Dict[str, Any]]] = None
_USB_HUB_PORT_CONTROLLER: Optional[Callable[[int, Dict[str, Any]], Dict[str, Any]]] = (
    None
)
_USB_HUB_CONTROLLER: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None


class SelectAudioPayload(BaseModel):
    device_id: str
    audio_device_name: str

    @field_validator("device_id")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        if not _DEVICE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "device_id contains characters that are unsafe in an MQTT topic"
            )
        return value


class USBHubPortCommand(BaseModel):
    enabled: Optional[bool] = None
    power_enabled: Optional[bool] = None
    data_enabled: Optional[bool] = None
    usb2_data_enabled: Optional[bool] = None
    usb3_data_enabled: Optional[bool] = None
    current_limit_ma: Optional[int] = Field(default=None, ge=0, le=4095)
    port_mode: Optional[Literal[0, 1]] = None
    action: Optional[Literal["reset_data", "reset_power", "clear_errors"]] = None

    @model_validator(mode="after")
    def validate_command(self):
        values = self.model_dump(exclude_none=True)
        if not values:
            raise ValueError("At least one USB hub setting or action is required")
        if self.action is not None and len(values) != 1:
            raise ValueError("USB hub actions cannot be combined with settings")
        return self


class USBHubCommand(BaseModel):
    active_upstream: Optional[Literal[0, 1]] = None
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def one_hub_setting(self):
        if len(self.model_dump(exclude_none=True)) != 1:
            raise ValueError("Exactly one USB hub setting is required")
        return self


def configure_command_publisher(
    publisher: Optional[Callable[[str, Any], bool]],
) -> None:
    """Connect the REST command endpoint to the controller's MQTT client."""
    global _COMMAND_PUBLISHER
    _COMMAND_PUBLISHER = publisher


def configure_usb_hub_api(
    status_provider: Optional[Callable[[], Dict[str, Any]]],
    port_controller: Optional[Callable[[int, Dict[str, Any]], Dict[str, Any]]],
    hub_controller: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
) -> None:
    """Connect live USB hub REST endpoints to the controller instance."""
    global _USB_HUB_STATUS_PROVIDER, _USB_HUB_PORT_CONTROLLER
    global _USB_HUB_CONTROLLER
    _USB_HUB_STATUS_PROVIDER = status_provider
    _USB_HUB_PORT_CONTROLLER = port_controller
    _USB_HUB_CONTROLLER = hub_controller


def ingest_mqtt_telemetry(topic: str, raw_payload: str) -> bool:
    """Validate a workstation telemetry message and add it to the REST cache."""
    topic_parts = topic.split("/")
    if (
        len(topic_parts) != 3
        or topic_parts[0] != "desk"
        or topic_parts[2] != "telemetry"
    ):
        return False

    try:
        payload = json.loads(raw_payload)
        telemetry = TelemetryMetrics.model_validate(payload)
    except (json.JSONDecodeError, ValueError, TypeError):
        return False

    topic_device_id = topic_parts[1]
    if (
        not _DEVICE_ID_PATTERN.fullmatch(topic_device_id)
        or telemetry.device_id != topic_device_id
    ):
        return False

    data = telemetry.model_dump(mode="json")
    data["last_updated"] = time.time()
    with _TELEMETRY_LOCK:
        _TELEMETRY_DB[telemetry.device_id] = data
    return True


def telemetry_snapshot() -> Dict[str, Dict[str, Any]]:
    """Return a copy that can be serialized without holding the MQTT lock."""
    with _TELEMETRY_LOCK:
        return deepcopy(_TELEMETRY_DB)


def clear_telemetry() -> None:
    """Clear cached telemetry; primarily useful for tests."""
    with _TELEMETRY_LOCK:
        _TELEMETRY_DB.clear()


@app.get("/")
def read_root():
    return {
        "status": "online",
        "app": "Desk Controller MQTT Bridge API",
        "configuration": "/config",
    }


@app.get("/api/v1/telemetry")
def get_all_telemetry():
    return telemetry_snapshot()


@app.get(
    "/api/v1/usb-hub",
    dependencies=[Depends(_require_lan_request)],
)
def get_usb_hub():
    if _USB_HUB_STATUS_PROVIDER is None:
        raise HTTPException(status_code=503, detail="USB hub is unavailable")
    return _USB_HUB_STATUS_PROVIDER()


@app.put(
    "/api/v1/usb-hub",
    dependencies=[Depends(_require_same_origin)],
)
def control_usb_hub(command: USBHubCommand):
    if _USB_HUB_CONTROLLER is None:
        raise HTTPException(status_code=503, detail="USB hub is unavailable")
    result = _USB_HUB_CONTROLLER(command.model_dump())
    if not result.get("success"):
        raise HTTPException(
            status_code=409,
            detail=result.get("error") or "USB hub rejected the command",
        )
    return result


@app.put(
    "/api/v1/usb-hub/ports/{port}",
    dependencies=[Depends(_require_same_origin)],
)
def control_usb_hub_port(port: int, command: USBHubPortCommand):
    if not 0 <= port <= 7:
        raise HTTPException(status_code=404, detail="USB port does not exist")
    if _USB_HUB_PORT_CONTROLLER is None:
        raise HTTPException(status_code=503, detail="USB hub is unavailable")
    result = _USB_HUB_PORT_CONTROLLER(
        port,
        command.model_dump(exclude_none=True),
    )
    if not result.get("success"):
        raise HTTPException(
            status_code=409,
            detail=result.get("error") or "USB hub rejected the command",
        )
    return result


@app.post("/api/v1/audio/select")
def set_active_audio_device(payload: SelectAudioPayload):
    if _COMMAND_PUBLISHER is None:
        raise HTTPException(
            status_code=503, detail="MQTT command bridge is unavailable"
        )

    topic = f"desk/{payload.device_id}/audio/set"
    if not _COMMAND_PUBLISHER(topic, payload.audio_device_name):
        raise HTTPException(status_code=503, detail="MQTT broker is unavailable")

    return {
        "status": "published",
        "topic": topic,
        "target_device": payload.audio_device_name,
    }
