"""Create credentials for a fresh Pi-hosted MQTT broker."""

import argparse
import secrets
from pathlib import Path
from typing import Tuple

from desk_controller.config import load_config, save_config


def ensure_local_mqtt_credentials(config_path: Path) -> Tuple[str, str]:
    """Create or preserve the shared credentials in a local-broker config."""
    config = load_config(str(config_path))
    mqtt = config.setdefault("mqtt", {})
    if str(mqtt.get("mode", "")).strip().lower() != "local":
        raise ValueError("MQTT credentials can only be bootstrapped in local mode")

    username = str(mqtt.get("username", "")).strip() or "desk-controller"
    password = str(mqtt.get("password", "")) or secrets.token_urlsafe(32)
    mqtt.update(
        {
            "broker": "127.0.0.1",
            "port": 1883,
            "username": username,
            "password": password,
        }
    )
    save_config(config, config_path)
    return username, password


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create credentials for a fresh Pi-hosted MQTT broker."
    )
    parser.add_argument("config_path", type=Path)
    args = parser.parse_args()

    username, password = ensure_local_mqtt_credentials(args.config_path)
    print(username)
    print(password)


if __name__ == "__main__":
    main()
