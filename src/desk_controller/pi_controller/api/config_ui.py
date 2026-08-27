"""LAN-only configuration API for the Raspberry Pi controller."""

import ipaddress
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from desk_controller.config import load_config, save_config
from desk_controller.pi_controller.streamdeck_layout import (
    configured_streamdeck_buttons,
    configured_usb_ports,
)

router = APIRouter()

_CONFIG_LOCK = threading.RLock()
_CONFIG_PATH: Optional[Path] = None
_RESTART_CALLBACK: Optional[Callable[[], None]] = None
_CONNECTION_STATUS_PROVIDER: Optional[Callable[[], Dict[str, Any]]] = None
_WEB_ROOT = Path(__file__).with_name("web")
_DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
_INPUT_PATTERN = re.compile(r"^(?:0x)?[0-9A-Fa-f]{1,2}$")
_LAN_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "fc00::/7",
        "fe80::/10",
    )
)


class MQTTSettings(BaseModel):
    mode: Literal["external", "local"] = "external"
    broker: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    username: str = Field(default="", max_length=256)
    password: Optional[str] = Field(default=None, max_length=1024)
    clear_password: bool = False

    @field_validator("broker")
    @classmethod
    def strip_broker(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("broker is required")
        return value


class HomeAssistantSettings(BaseModel):
    enabled: bool = True
    url: str = Field(default="", max_length=2048)
    token: Optional[str] = Field(default=None, max_length=4096)
    clear_token: bool = False

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value:
            return value
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Home Assistant URL must start with http:// or https://")
        return value

    @model_validator(mode="after")
    def require_url_when_enabled(self):
        if self.enabled and not self.url:
            raise ValueError(
                "Home Assistant URL is required when the integration is enabled"
            )
        return self


class ServerSettings(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)


class HardwareSettings(BaseModel):
    simulate: bool = False


class KVMCoordinatorSettings(BaseModel):
    monitor_controller: Literal[
        "pi",
        "active_workstation",
        "pc1",
        "pc2",
    ] = "pi"
    usb_controller: Literal[
        "pi",
        "active_workstation",
        "pc1",
        "pc2",
    ] = "pi"
    remote_timeout: int = Field(default=10, ge=2, le=30)


class AcronameSettings(BaseModel):
    serial_number: Optional[str] = Field(default=None, max_length=32)
    default_channel: int = Field(ge=0, le=1)

    @field_validator("serial_number")
    @classmethod
    def validate_serial_number(cls, value: Optional[str]) -> Optional[str]:
        if value is None or not value.strip():
            return None
        value = value.strip()
        try:
            int(value, 0)
        except ValueError as exc:
            raise ValueError("serial number must be decimal or start with 0x") from exc
        return value


class USBSwitchSettings(BaseModel):
    driver: Literal["acroname", "ugreen_cm691_gpio"] = "acroname"
    default_channel: int = Field(default=0, ge=0, le=1)
    gpio_pin: int = Field(default=17, ge=2, le=27)
    gpio_active_high: bool = False
    gpio_pulse_ms: int = Field(default=200, ge=50, le=2000)
    observation_enabled: bool = False
    sentinel_vendor_id: Optional[int] = Field(
        default=None,
        ge=0,
        le=0xFFFF,
    )
    sentinel_product_id: Optional[int] = Field(
        default=None,
        ge=0,
        le=0xFFFF,
    )
    sentinel_serial_number: Optional[str] = Field(
        default=None,
        max_length=256,
    )
    observation_poll_interval: int = Field(default=2, ge=1, le=30)
    observation_timeout: int = Field(default=8, ge=3, le=60)

    @field_validator(
        "sentinel_vendor_id",
        "sentinel_product_id",
        mode="before",
    )
    @classmethod
    def parse_usb_id(cls, value):
        if value is None or value == "":
            return None
        if isinstance(value, str):
            try:
                return int(value.strip(), 0)
            except ValueError as exc:
                raise ValueError("USB IDs must be decimal or start with 0x") from exc
        return value

    @field_validator("sentinel_serial_number")
    @classmethod
    def normalize_sentinel_serial(
        cls,
        value: Optional[str],
    ) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @model_validator(mode="after")
    def require_complete_sentinel(self):
        if (
            self.driver == "ugreen_cm691_gpio"
            and self.observation_enabled
            and (self.sentinel_vendor_id is None or self.sentinel_product_id is None)
        ):
            raise ValueError(
                "sentinel vendor and product IDs are required when "
                "USB observation is enabled"
            )
        return self


class MonitorSettings(BaseModel):
    display_id: int = Field(ge=1, le=64)
    pc1_input: str
    pc2_input: str

    @field_validator("pc1_input", "pc2_input")
    @classmethod
    def validate_input(cls, value: str) -> str:
        value = value.strip()
        if not _INPUT_PATTERN.fullmatch(value):
            raise ValueError("monitor inputs must be one-byte hexadecimal values")
        return f"0x{int(value, 16):02x}"


class WorkstationSettings(BaseModel):
    pc1: str = Field(min_length=1, max_length=128)
    pc2: str = Field(min_length=1, max_length=128)

    @field_validator("pc1", "pc2")
    @classmethod
    def validate_device_id(cls, value: str) -> str:
        value = value.strip()
        if not _DEVICE_ID_PATTERN.fullmatch(value):
            raise ValueError(
                "workstation IDs may only contain letters, numbers, dots, dashes, and underscores"
            )
        return value


class StreamDeckButtonSettings(BaseModel):
    key: int = Field(ge=0, le=14)
    enabled: bool = True
    label: str = Field(default="", max_length=128)
    icon: str = Field(default="headphones", max_length=64)
    accent_color: List[int] = Field(
        default_factory=lambda: [0, 200, 255],
        min_length=3,
        max_length=3,
    )
    group: str = Field(default="", max_length=64)
    action_type: Literal[
        "none",
        "kvm_toggle",
        "current_time",
        "current_date",
        "audio_output",
        "ha_scene",
        "ha_service",
        "mqtt",
        "workstation_slot",
    ] = "none"
    slot_id: str = Field(default="", max_length=64)
    target: str = Field(default="", max_length=1024)
    off_target: str = Field(default="", max_length=1024)
    mqtt_topic: str = Field(default="", max_length=512)
    mqtt_payload: str = Field(default="", max_length=4096)
    state_topic: str = Field(default="", max_length=512)
    state_payload: str = Field(default="", max_length=4096)
    service: str = Field(default="", max_length=128)
    service_data: Dict[str, Any] = Field(default_factory=dict)

    @field_validator("accent_color")
    @classmethod
    def validate_color(cls, value: List[int]) -> List[int]:
        if any(component < 0 or component > 255 for component in value):
            raise ValueError("accent color components must be between 0 and 255")
        return value

    @field_validator(
        "label",
        "icon",
        "group",
        "target",
        "off_target",
        "mqtt_topic",
        "state_topic",
        "slot_id",
        "service",
    )
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("mqtt_topic", "state_topic")
    @classmethod
    def validate_topic(cls, value: str) -> str:
        if "\x00" in value or "+" in value or "#" in value:
            raise ValueError("button MQTT topics must be exact topics")
        return value

    @model_validator(mode="after")
    def validate_action(self):
        if not self.enabled:
            return self
        if self.action_type == "audio_output" and not self.target:
            raise ValueError("audio buttons require an output-device target")
        if self.action_type == "ha_scene" and not self.target.startswith("scene."):
            raise ValueError("Home Assistant scene targets must start with scene.")
        if self.action_type == "ha_service":
            parts = self.service.split(".")
            if len(parts) != 2 or not all(
                re.fullmatch(r"[a-z0-9_]+", part) for part in parts
            ):
                raise ValueError("Home Assistant services must use domain.service")
        if self.action_type == "mqtt" and not self.mqtt_topic:
            raise ValueError("MQTT buttons require a publish topic")
        if self.action_type == "workstation_slot":
            if not self.slot_id or not _DEVICE_ID_PATTERN.fullmatch(self.slot_id):
                raise ValueError(
                    "workstation slots require an ID containing only letters, "
                    "numbers, dots, dashes, and underscores"
                )
        if bool(self.state_topic) != bool(self.state_payload):
            raise ValueError(
                "state topic and active payload must be configured together"
            )
        return self


class StreamDeckSettings(BaseModel):
    brightness: int = Field(ge=0, le=100)
    pending_request_timeout: int = Field(default=300, ge=5, le=86400)
    pending_retry_interval: int = Field(default=5, ge=1, le=3600)
    buttons: List[StreamDeckButtonSettings] = Field(
        default_factory=list,
        max_length=15,
    )

    @model_validator(mode="after")
    def unique_button_positions(self):
        positions = [button.key for button in self.buttons]
        if len(positions) != len(set(positions)):
            raise ValueError("Stream Deck button positions must be unique")
        return self


class USBPortSettings(BaseModel):
    index: int = Field(ge=0, le=7)
    name: str = Field(min_length=1, max_length=128)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("USB port name is required")
        return value


class USBHubSettings(BaseModel):
    telemetry_interval: int = Field(default=10, ge=2, le=3600)
    ports: List[USBPortSettings] = Field(min_length=8, max_length=8)

    @model_validator(mode="after")
    def all_port_positions(self):
        positions = sorted(port.index for port in self.ports)
        if positions != list(range(8)):
            raise ValueError("USB hub configuration must name ports 0 through 7")
        return self


class PiConfigurationUpdate(BaseModel):
    server: ServerSettings
    mqtt: MQTTSettings
    homeassistant: HomeAssistantSettings
    hardware: HardwareSettings
    kvm: KVMCoordinatorSettings = Field(default_factory=KVMCoordinatorSettings)
    usb_switch: Optional[USBSwitchSettings] = None
    acroname: AcronameSettings
    monitor: MonitorSettings
    streamdeck: StreamDeckSettings
    usb_hub: USBHubSettings
    workstations: WorkstationSettings


def configure_config_ui(
    config_path: Path,
    restart_callback: Optional[Callable[[], None]] = None,
    connection_status_provider: Optional[Callable[[], Dict[str, Any]]] = None,
) -> None:
    """Connect the web editor to the controller's active config file."""
    global _CONFIG_PATH, _RESTART_CALLBACK, _CONNECTION_STATUS_PROVIDER
    _CONFIG_PATH = Path(config_path)
    _RESTART_CALLBACK = restart_callback
    _CONNECTION_STATUS_PROVIDER = connection_status_provider


def _canonical_host(value: str) -> str:
    value = value.strip().rstrip(".").lower()
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.compressed


def _is_lan_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_loopback or any(
        address.version == network.version and address in network
        for network in _LAN_NETWORKS
    )


def _require_lan_request(request: Request) -> None:
    client_host = request.client.host if request.client else ""
    if client_host == "testclient":
        return
    if not _is_lan_address(client_host):
        raise HTTPException(
            status_code=403,
            detail="Configuration is available only from the local network",
        )


def _require_same_origin(request: Request) -> None:
    _require_lan_request(request)
    origin = request.headers.get("origin")
    if not origin:
        return

    try:
        parsed_origin = urlparse(origin)
        origin_host = parsed_origin.hostname or ""
        origin_port = parsed_origin.port
    except ValueError as exc:
        raise HTTPException(
            status_code=403,
            detail="Invalid configuration origin",
        ) from exc

    request_host = request.url.hostname or ""
    request_port = request.url.port
    if parsed_origin.scheme not in {"http", "https"}:
        raise HTTPException(status_code=403, detail="Invalid configuration origin")

    origin_port = origin_port or (443 if parsed_origin.scheme == "https" else 80)
    request_port = request_port or (443 if request.url.scheme == "https" else 80)
    if (
        parsed_origin.scheme != request.url.scheme
        or _canonical_host(origin_host) != _canonical_host(request_host)
        or origin_port != request_port
    ):
        raise HTTPException(status_code=403, detail="Invalid configuration origin")


def _active_config_path() -> Path:
    if _CONFIG_PATH is None:
        raise HTTPException(
            status_code=503, detail="Configuration editor is unavailable"
        )
    return _CONFIG_PATH


def _serial_for_display(value) -> str:
    if value is None:
        return ""
    if isinstance(value, int):
        return f"0x{value:08x}"
    return str(value)


def _usb_id_for_display(value) -> str:
    if value is None:
        return ""
    try:
        return f"0x{int(value):04x}"
    except (TypeError, ValueError):
        return str(value)


def _public_config() -> Dict:
    path = _active_config_path()
    with _CONFIG_LOCK:
        config = load_config(str(path))

    mqtt = config.get("mqtt", {})
    homeassistant = config.get("homeassistant", {})
    monitors = config.get("monitors", [{}])
    monitor = monitors[0] if monitors else {}
    inputs = monitor.get("inputs", {})
    acroname = config.get("acroname", {})
    usb_switch = config.get("usb_switch", {})
    buttons = configured_streamdeck_buttons(config)
    ports = configured_usb_ports(config)
    usb_hub = config.get("usb_hub", {})

    return {
        "server": config.get("server", {}),
        "mqtt": {
            "mode": mqtt.get("mode", "external"),
            "broker": mqtt.get("broker", "homeassistant.local"),
            "port": mqtt.get("port", 1883),
            "username": mqtt.get("username", ""),
            "password_configured": bool(mqtt.get("password")),
        },
        "homeassistant": {
            "enabled": homeassistant.get("enabled", True),
            "url": homeassistant.get(
                "url",
                "http://homeassistant.local:8123",
            ),
            "token_configured": bool(homeassistant.get("token")),
        },
        "hardware": config.get("hardware", {}),
        "kvm": config.get(
            "kvm",
            {
                "monitor_controller": "pi",
                "usb_controller": "pi",
                "remote_timeout": 10,
            },
        ),
        "acroname": {
            "serial_number": _serial_for_display(acroname.get("serial_number")),
            "default_channel": acroname.get("default_channel", 0),
        },
        "usb_switch": {
            "driver": usb_switch.get("driver", "acroname"),
            "default_channel": usb_switch.get(
                "default_channel",
                acroname.get("default_channel", 0),
            ),
            "gpio_pin": usb_switch.get("gpio_pin", 17),
            "gpio_active_high": usb_switch.get(
                "gpio_active_high",
                False,
            ),
            "gpio_pulse_ms": usb_switch.get("gpio_pulse_ms", 200),
            "observation_enabled": usb_switch.get(
                "observation_enabled",
                False,
            ),
            "sentinel_vendor_id": _usb_id_for_display(
                usb_switch.get("sentinel_vendor_id")
            ),
            "sentinel_product_id": _usb_id_for_display(
                usb_switch.get("sentinel_product_id")
            ),
            "sentinel_serial_number": usb_switch.get("sentinel_serial_number"),
            "observation_poll_interval": usb_switch.get(
                "observation_poll_interval",
                2,
            ),
            "observation_timeout": usb_switch.get(
                "observation_timeout",
                8,
            ),
        },
        "monitor": {
            "display_id": monitor.get("display_id", 1),
            "pc1_input": inputs.get("pc1", "0x0f"),
            "pc2_input": inputs.get("pc2", "0x11"),
        },
        "streamdeck": {
            "brightness": config.get("streamdeck", {}).get(
                "brightness",
                85,
            ),
            "pending_request_timeout": config.get("streamdeck", {}).get(
                "pending_request_timeout",
                300,
            ),
            "pending_retry_interval": config.get("streamdeck", {}).get(
                "pending_retry_interval",
                5,
            ),
            "buttons": [
                {"key": key, **button} for key, button in sorted(buttons.items())
            ],
        },
        "usb_hub": {
            "telemetry_interval": usb_hub.get("telemetry_interval", 10),
            "ports": [
                {"index": index, **values} for index, values in sorted(ports.items())
            ],
        },
        "workstations": config.get(
            "workstations",
            {"pc1": "computer-one", "pc2": "computer-two"},
        ),
    }


def _save_update(payload: PiConfigurationUpdate) -> Path:
    path = _active_config_path()
    with _CONFIG_LOCK:
        config = load_config(str(path))

        config.setdefault("server", {}).update(payload.server.model_dump())

        mqtt = config.setdefault("mqtt", {})
        mqtt.update(
            {
                "mode": payload.mqtt.mode,
                "broker": payload.mqtt.broker,
                "port": payload.mqtt.port,
                "username": payload.mqtt.username,
            }
        )
        if payload.mqtt.clear_password:
            mqtt["password"] = ""
        elif payload.mqtt.password:
            mqtt["password"] = payload.mqtt.password

        homeassistant = config.setdefault("homeassistant", {})
        homeassistant["enabled"] = payload.homeassistant.enabled
        homeassistant["url"] = payload.homeassistant.url
        if payload.homeassistant.clear_token:
            homeassistant["token"] = ""
        elif payload.homeassistant.token:
            homeassistant["token"] = payload.homeassistant.token

        config["hardware"] = payload.hardware.model_dump()
        config["kvm"] = payload.kvm.model_dump()
        config["acroname"] = {
            "serial_number": (
                int(payload.acroname.serial_number, 0)
                if payload.acroname.serial_number
                else None
            ),
            "default_channel": (
                payload.usb_switch.default_channel
                if payload.usb_switch is not None
                else payload.acroname.default_channel
            ),
        }
        if payload.usb_switch is not None:
            config["usb_switch"] = payload.usb_switch.model_dump()
        config["monitors"] = [
            {
                "display_id": payload.monitor.display_id,
                "inputs": {
                    "pc1": payload.monitor.pc1_input,
                    "pc2": payload.monitor.pc2_input,
                },
            }
        ]
        streamdeck = config.setdefault("streamdeck", {})
        streamdeck["brightness"] = payload.streamdeck.brightness
        streamdeck["pending_request_timeout"] = (
            payload.streamdeck.pending_request_timeout
        )
        streamdeck["pending_retry_interval"] = payload.streamdeck.pending_retry_interval
        streamdeck["buttons"] = {
            button.key: button.model_dump(exclude={"key"})
            for button in payload.streamdeck.buttons
        }
        streamdeck.pop("keys", None)
        config.pop("audio_devices", None)
        config["usb_hub"] = {
            "telemetry_interval": payload.usb_hub.telemetry_interval,
            "ports": {
                port.index: {"name": port.name} for port in payload.usb_hub.ports
            },
        }
        config["workstations"] = payload.workstations.model_dump()

        return save_config(config, path)


@router.get("/config", dependencies=[Depends(_require_lan_request)])
def configuration_page():
    return FileResponse(
        _WEB_ROOT / "index.html",
        media_type="text/html",
        headers={
            "Cache-Control": "no-store",
            "Content-Security-Policy": (
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; "
                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
        },
    )


@router.get(
    "/config/assets/styles.css",
    dependencies=[Depends(_require_lan_request)],
)
def configuration_styles():
    return FileResponse(
        _WEB_ROOT / "styles.css",
        media_type="text/css",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/config/assets/app.js",
    dependencies=[Depends(_require_lan_request)],
)
def configuration_script():
    return FileResponse(
        _WEB_ROOT / "app.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/api/v1/config",
    dependencies=[Depends(_require_lan_request)],
)
def get_configuration():
    return _public_config()


@router.get(
    "/api/v1/config/status",
    dependencies=[Depends(_require_lan_request)],
)
def get_connection_status():
    if _CONNECTION_STATUS_PROVIDER is None:
        raise HTTPException(
            status_code=503,
            detail="Connection status is unavailable",
        )
    return _CONNECTION_STATUS_PROVIDER()


@router.put(
    "/api/v1/config",
    dependencies=[Depends(_require_same_origin)],
)
def update_configuration(payload: PiConfigurationUpdate):
    saved_path = _save_update(payload)
    return {
        "status": "saved",
        "restart_required": True,
        "path": str(saved_path),
    }


@router.post(
    "/api/v1/config/restart",
    dependencies=[Depends(_require_same_origin)],
)
def restart_controller():
    if _RESTART_CALLBACK is None:
        raise HTTPException(status_code=503, detail="Restart is unavailable")
    _RESTART_CALLBACK()
    return {"status": "restarting"}
