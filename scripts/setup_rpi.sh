#!/usr/bin/env bash

# Setup script for Raspberry Pi 4 Smart Desk & KVM Controller
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$(id -un)}"
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
SERVICE_TEMPLATE="$PROJECT_DIR/systemd/desk-controller.service"
SERVICE_DESTINATION="/etc/systemd/system/desk-controller.service"
UDEV_RULES="$PROJECT_DIR/systemd/99-desk-controller.rules"
CREDENTIALS_FILE=""

cleanup() {
    if [[ -n "$CREDENTIALS_FILE" ]]; then
        rm -f -- "$CREDENTIALS_FILE"
    fi
}
trap cleanup EXIT

if [[ "$PROJECT_DIR" == *" "* ]]; then
    echo "Project paths containing spaces are not supported by the systemd installer: $PROJECT_DIR" >&2
    exit 2
fi

echo "=== Installing system dependencies ==="
sudo apt-get update
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    libusb-1.0-0-dev \
    libhidapi-libusb0 \
    fonts-dejavu-core \
    ddcutil \
    i2c-tools \
    mosquitto \
    mosquitto-clients \
    rsync

echo "=== Adding user to hardware-access groups ==="
sudo usermod -aG gpio,i2c,plugdev "$SERVICE_USER"

echo "=== Configuring udev permissions for Stream Deck & Acroname Hub ==="
sudo install -m 0644 "$UDEV_RULES" /etc/udev/rules.d/99-desk-controller.rules
sudo udevadm control --reload-rules
sudo udevadm trigger

echo "=== Setting up Python Virtual Environment ==="
python3 -m venv "$PROJECT_DIR/venv"
"$PROJECT_DIR/venv/bin/python" -m pip install --upgrade pip
"$PROJECT_DIR/venv/bin/python" -m pip install -e "${PROJECT_DIR}[pi,acroname]"

if [[ ! -f "$PROJECT_DIR/config/config.yaml" ]]; then
    sudo install \
        -o "$SERVICE_USER" \
        -g "$SERVICE_GROUP" \
        -m 0600 \
        "$PROJECT_DIR/config/config.example.yaml" \
        "$PROJECT_DIR/config/config.yaml"
    echo "Created $PROJECT_DIR/config/config.yaml."
fi

echo "=== Configuring optional Pi-hosted MQTT broker ==="
MQTT_MODE="$(
    "$PROJECT_DIR/venv/bin/python" -c \
        'from desk_controller.config import load_config; import sys; print(load_config(sys.argv[1])["mqtt"]["mode"])' \
        "$PROJECT_DIR/config/config.yaml"
)"
if [[ "$MQTT_MODE" == "local" ]]; then
    DESK_BROKER_CONFIG="/etc/mosquitto/conf.d/desk-controller.conf"
    if [[ -f "$DESK_BROKER_CONFIG" ]] || \
        ! sudo grep -RqsE '^[[:space:]]*listener[[:space:]]+' \
            /etc/mosquitto/mosquitto.conf /etc/mosquitto/conf.d; then
        mapfile -t MQTT_CREDENTIALS < <(
            "$PROJECT_DIR/venv/bin/python" \
                -m desk_controller.mqtt_bootstrap \
                "$PROJECT_DIR/config/config.yaml"
        )
        MQTT_USERNAME="${MQTT_CREDENTIALS[0]:-}"
        MQTT_PASSWORD="${MQTT_CREDENTIALS[1]:-}"
        if [[ -z "$MQTT_USERNAME" || -z "$MQTT_PASSWORD" ]] || \
            [[ "$MQTT_USERNAME" == *:* || "$MQTT_PASSWORD" == *:* ]] || \
            [[ "$MQTT_USERNAME" == *$'\n'* || "$MQTT_PASSWORD" == *$'\n'* ]]; then
            echo "MQTT username and password must be non-empty and cannot contain colons or newlines." >&2
            exit 2
        fi

        CREDENTIALS_FILE="$(mktemp)"
        chmod 0600 "$CREDENTIALS_FILE"
        printf '%s:%s\n' "$MQTT_USERNAME" "$MQTT_PASSWORD" >"$CREDENTIALS_FILE"
        sudo install \
            -o root \
            -g mosquitto \
            -m 0640 \
            "$CREDENTIALS_FILE" \
            /etc/mosquitto/passwd.desk-controller
        rm -f -- "$CREDENTIALS_FILE"
        CREDENTIALS_FILE=""
        sudo mosquitto_passwd -U /etc/mosquitto/passwd.desk-controller

        sudo tee "$DESK_BROKER_CONFIG" >/dev/null <<'EOF'
# Authenticated desk-controller broker for the private LAN.
per_listener_settings true
listener 1883
allow_anonymous false
password_file /etc/mosquitto/passwd.desk-controller
EOF
        echo "Configured an authenticated LAN listener for the Pi-hosted broker."
    else
        echo "Preserving the existing Mosquitto listener configuration."
        echo "Update config/config.yaml with credentials accepted by that listener."
    fi
    sudo systemctl enable mosquitto.service
    sudo systemctl restart mosquitto.service
else
    echo "External MQTT mode selected; the local Mosquitto service was not enabled."
fi

echo "=== Installing systemd Service ==="
sed \
    -e "s|@SERVICE_USER@|$SERVICE_USER|g" \
    -e "s|@SERVICE_GROUP@|$SERVICE_GROUP|g" \
    -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
    "$SERVICE_TEMPLATE" | sudo tee "$SERVICE_DESTINATION" >/dev/null
sudo chmod 0644 "$SERVICE_DESTINATION"
sudo systemctl daemon-reload
sudo systemctl enable desk-controller.service
sudo systemctl restart desk-controller.service
echo "Desk Controller service is enabled and running."
echo "Open the configuration UI from any browser on your private LAN:"
echo "  http://<raspberry-pi-hostname>.local:8080/config"

echo "=== RPi Setup Complete (log out and back in to apply group membership) ==="
