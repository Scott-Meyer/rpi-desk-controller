"""
Centralized Configuration Parser for Desk Controller and Desktop Agent.
"""

import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

import yaml

logger = logging.getLogger(__name__)


def user_config_path() -> Path:
    """Return the per-user configuration path for this operating system."""
    if sys.platform == "win32":
        base_dir = Path(os.environ.get("APPDATA", Path.home()))
        return base_dir / "DeskController" / "config.yaml"
    if sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "DeskController"
            / "config.yaml"
        )

    base_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base_dir / "desk-controller" / "config.yaml"


def _user_config_path() -> Path:
    """Backward-compatible private alias for older callers."""
    return user_config_path()


def _resolve_config_path(config_path: Optional[str]) -> Path:
    if config_path:
        return Path(config_path).expanduser()

    env_path = os.environ.get("DESK_CONTROLLER_CONFIG")
    if env_path:
        return Path(env_path).expanduser()

    candidates = [Path.cwd() / "config" / "config.yaml"]
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates.extend(
            [
                executable_dir / "config" / "config.yaml",
                executable_dir / "config.yaml",
            ]
        )
    candidates.append(user_config_path())

    return next((path for path in candidates if path.is_file()), candidates[0])


def resolve_config_path(config_path: Optional[str] = None) -> Path:
    """Return the path that ``load_config`` will read."""
    return _resolve_config_path(config_path)


def config_exists(config_path: Optional[str] = None) -> bool:
    """Return whether a configuration file currently exists."""
    return resolve_config_path(config_path).is_file()


def load_config(config_path: Optional[str] = None) -> Dict[str, Any]:
    """Loads configuration YAML file with robust fallback defaults."""
    defaults = {
        "server": {"host": "127.0.0.1", "port": 8080},
        "homeassistant": {
            "enabled": False,
            "url": "http://homeassistant.local:8123",
            "token": "",
        },
        "mqtt": {
            "mode": "external",
            "broker": "homeassistant.local",
            "port": 1883,
            "username": "",
            "password": "",
        },
        "hardware": {
            "simulate": False,
        },
        "kvm": {
            "monitor_controller": "pi",
            "usb_controller": "pi",
            "remote_timeout": 10,
        },
        "usb_switch": {
            "driver": "acroname",
            "default_channel": 0,
            "gpio_pin": 17,
            "gpio_active_high": False,
            "gpio_pulse_ms": 200,
            "observation_enabled": False,
            "sentinel_vendor_id": None,
            "sentinel_product_id": None,
            "sentinel_serial_number": None,
            "observation_poll_interval": 2,
            "observation_timeout": 8,
        },
        "acroname": {"serial_number": None, "default_channel": 0},
        "monitors": [{"display_id": 1, "inputs": {"pc1": "0x0f", "pc2": "0x11"}}],
        "streamdeck": {
            "brightness": 70,
            "pending_request_timeout": 300,
            "pending_retry_interval": 5,
            "buttons": {},
            "keys": {},
        },
        "usb_hub": {
            "telemetry_interval": 10,
            "ports": {port: {"name": f"USB Port {port}"} for port in range(8)},
        },
        "workstations": {
            "pc1": "computer-one",
            "pc2": "computer-two",
        },
        "workstation_slots": {},
        "desktop_kvm": {
            "hotkey": {
                "enabled": False,
                "shortcut": "ctrl+option+command+k",
            },
            "monitor": {
                "enabled": False,
                "backend": "auto",
                "display_name": "",
                "betterdisplay_path": (
                    "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
                ),
                "display_id": 1,
                "inputs": {
                    "pc1": "0x0f",
                    "pc2": "0x11",
                },
            },
            "usb": {
                "enabled": False,
                "serial_number": None,
            },
        },
        "audio_devices": {},
    }

    resolved_path = _resolve_config_path(config_path)
    if resolved_path.is_file():
        try:
            with resolved_path.open("r", encoding="utf-8") as f:
                user_conf = yaml.safe_load(f) or {}
                # Deep merge user_conf over defaults
                for key, val in user_conf.items():
                    if (
                        isinstance(val, dict)
                        and key in defaults
                        and isinstance(defaults[key], dict)
                    ):
                        defaults[key].update(val)
                    else:
                        defaults[key] = val
        except Exception as e:
            logger.error(f"Failed loading configuration file '{resolved_path}': {e}")
    else:
        logger.warning(
            "Config file '%s' not found. Set DESK_CONTROLLER_CONFIG or create the file; "
            "using default values.",
            resolved_path,
        )

    return defaults


def normalize_mqtt_config(settings: Mapping[str, Any]) -> Dict[str, Any]:
    """Validate and normalize values accepted by the desktop settings UI."""
    broker = str(settings.get("broker", "")).strip()
    if not broker:
        raise ValueError("MQTT broker is required")

    try:
        port = int(settings.get("port", 1883))
    except (TypeError, ValueError) as exc:
        raise ValueError("MQTT port must be a number") from exc
    if not 1 <= port <= 65535:
        raise ValueError("MQTT port must be between 1 and 65535")

    username_value = settings.get("username", "")
    password_value = settings.get("password", "")
    return {
        "broker": broker,
        "port": port,
        "username": "" if username_value is None else str(username_value).strip(),
        "password": "" if password_value is None else str(password_value),
    }


def save_mqtt_config(
    settings: Mapping[str, Any],
    config_path: Optional[Union[str, Path]] = None,
) -> Path:
    """Atomically persist MQTT settings without exposing them to other users."""
    mqtt_config = normalize_mqtt_config(settings)
    target = (
        Path(config_path).expanduser()
        if config_path is not None
        else user_config_path()
    )

    existing: Dict[str, Any] = {}
    if target.is_file():
        try:
            with target.open("r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle) or {}
        except Exception as exc:
            raise ValueError(f"Could not read existing configuration: {exc}") from exc
        if not isinstance(loaded, dict):
            raise ValueError("Existing configuration must contain a YAML mapping")
        existing = loaded

    existing["mqtt"] = mqtt_config
    return save_config(existing, target)


def save_config(
    config: Mapping[str, Any],
    config_path: Union[str, Path],
) -> Path:
    """Atomically persist a complete configuration with owner-only access."""
    if not isinstance(config, Mapping):
        raise ValueError("Configuration must be a mapping")

    target = Path(config_path).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(target.parent, 0o700)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config-",
        suffix=".yaml.tmp",
        dir=str(target.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary_path, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(config), handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, target)
        if os.name != "nt":
            os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        temporary_path.unlink(missing_ok=True)
        raise

    return target
