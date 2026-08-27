"""
MQTT-Native Desktop Agent for Windows & macOS with Home Assistant Auto-Discovery.
"""

import json
import logging
import platform
import threading
import time
from copy import deepcopy
from typing import Any, Dict, Mapping, Optional

import psutil

from desk_controller import __version__
from desk_controller.config import load_config, normalize_mqtt_config
from desk_controller.core.kvm import (
    KVM_REQUEST_TOPIC,
    KVMHardwareCommand,
    KVMHardwareResult,
    KVMSwitchRequest,
    kvm_hardware_command_topic,
    kvm_hardware_result_topic,
)
from desk_controller.core.models import DeviceStatus, TelemetryMetrics
from desk_controller.core.mqtt_client import MQTTClientHelper
from desk_controller.core.network import get_lan_ip
from desk_controller.core.usb_observation import (
    USB_OBSERVATION_CONFIG_TOPIC,
    USBPresenceObservation,
    USBSentinelConfig,
    usb_observation_topic,
)
from desk_controller.core.workstation_slots import (
    WorkstationSlotCommand,
    WorkstationSlotManifest,
    WorkstationSlotResult,
    WorkstationSlotState,
    availability_topic,
    slot_command_topic,
    slot_manifest_topic,
    slot_result_topic,
    slot_state_topic,
)
from desk_controller.desktop_agent.audio.macos import MacOSAudioDriver
from desk_controller.desktop_agent.audio.windows import WindowsAudioDriver
from desk_controller.desktop_agent.kvm_hardware import (
    DesktopKVMHardware,
    DesktopKVMSettings,
)
from desk_controller.desktop_agent.media import MacOSMediaDriver, NowPlayingState
from desk_controller.desktop_agent.usb_presence import USBPresenceProbe
from desk_controller.desktop_agent.workstation_buttons import (
    DESKTOP_ACTION_CATALOG,
    DesktopWorkstationButtonRegistry,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("DesktopAgent")


class DesktopAgent:
    STREAMDECK_LAYOUT_TOPIC = "desk/rpi_desk_controller/streamdeck/layout"
    ONLINE_POLL_INTERVAL = 2.0
    OFFLINE_POLL_INTERVAL = 30.0

    def __init__(self, config_path: Optional[str] = None):
        self.hostname = platform.node()
        self.os_type = platform.system().lower()  # "windows" or "darwin"
        self.device_id = self.hostname.lower().replace(" ", "_")
        self.lan_ip = None
        self._stop_event = threading.Event()
        self._poll_wake_event = threading.Event()
        self._connection_state_callback = None
        self._last_slot_manifest = None
        self._last_slot_state = None
        self._kvm_result_lock = threading.RLock()
        self._kvm_command_lock = threading.RLock()
        self._kvm_result_cache: Dict[str, Dict[str, Any]] = {}
        self._slot_command_lock = threading.RLock()
        self._slot_result_cache: Dict[str, Dict[str, Any]] = {}
        self._hotkey_reconfigure_callback = None
        self._layout_lock = threading.RLock()
        self._streamdeck_layout: Dict[str, Any] = {
            "rows": 3,
            "columns": 5,
            "buttons": [],
        }
        self._layout_received = False
        self._usb_observation_lock = threading.RLock()
        self._usb_sentinel_config = USBSentinelConfig()
        self._usb_presence_probe = USBPresenceProbe(self.os_type)
        self._last_usb_observation = 0.0

        config = load_config(config_path)
        mqtt_conf = config.get("mqtt", {})
        self.workstation_buttons = DesktopWorkstationButtonRegistry.from_config(
            config.get("workstation_slots", {}),
            self.os_type,
        )
        self.kvm_hardware = DesktopKVMHardware.from_config(
            config.get("desktop_kvm", {}),
            self.os_type,
        )
        if self.os_type == "windows":
            self.audio_driver = WindowsAudioDriver()
        elif self.os_type == "darwin":
            self.audio_driver = MacOSAudioDriver()
        else:
            self.audio_driver = None
        self.media_driver = None
        if self.os_type == "darwin":
            try:
                self.media_driver = MacOSMediaDriver()
            except Exception:
                logger.exception("macOS Now Playing integration is unavailable")

        self.mqtt = self._create_mqtt_client(mqtt_conf)

    def _create_mqtt_client(self, mqtt_conf):
        return MQTTClientHelper(
            client_id=f"desktop_agent_{self.device_id}",
            broker=mqtt_conf.get("broker", "homeassistant.local"),
            port=int(mqtt_conf.get("port", 1883)),
            username=mqtt_conf.get("username") or None,
            password=mqtt_conf.get("password") or None,
            on_message_callback=self._on_mqtt_message,
            on_connect_callback=self._on_mqtt_connected,
            on_connection_state_callback=self._on_mqtt_connection_state,
            will_topic=availability_topic(self.device_id),
            will_payload="offline",
            will_qos=1,
            will_retain=True,
        )

    def set_connection_state_callback(self, callback) -> None:
        """Observe MQTT connectivity and immediately receive the current state."""
        self._connection_state_callback = callback
        if callback is not None:
            callback(bool(self.mqtt.is_connected))

    def _on_mqtt_connection_state(self, connected: bool) -> None:
        """Wake offline polling and forward state to the desktop shell."""
        wake_event = getattr(self, "_poll_wake_event", None)
        if wake_event is not None:
            wake_event.set()
        callback = getattr(self, "_connection_state_callback", None)
        if callback is None:
            return
        try:
            callback(bool(connected))
        except Exception:
            logger.exception("Desktop connection-state callback failed")

    def _on_mqtt_connected(self):
        """Publish discovery and retained identity after every connection."""
        self.register_ha_discovery()
        self.lan_ip = get_lan_ip(self.mqtt.broker, self.mqtt.port)
        status = DeviceStatus(
            device_id=self.device_id,
            hostname=self.hostname,
            os_type=self.os_type,
            lan_ip=self.lan_ip,
            app_version=__version__,
        ).model_dump(mode="json")
        if not self.mqtt.publish(
            f"desk/{self.device_id}/status",
            status,
            retain=True,
        ):
            logger.warning("Failed publishing retained workstation status")
        # Publish fresh retained content before the online birth message so a
        # reconnect cannot briefly expose stale application state as current.
        self._publish_workstation_slots(force=True)
        self._publish_usb_observation(force=True)
        if not self.mqtt.publish(
            availability_topic(self.device_id),
            "online",
            qos=1,
            retain=True,
        ):
            logger.warning("Failed publishing workstation availability")

    def reconfigure_mqtt(self, settings) -> bool:
        """Apply persisted MQTT settings without restarting the desktop app."""
        mqtt_conf = normalize_mqtt_config(settings)
        replacement = self._create_mqtt_client(mqtt_conf)
        replacement.subscribe(f"desk/{self.device_id}/audio/set")
        replacement.subscribe(slot_command_topic(self.device_id), qos=1)
        replacement.subscribe(self.STREAMDECK_LAYOUT_TOPIC, qos=1)
        replacement.subscribe(
            kvm_hardware_command_topic(self.device_id),
            qos=1,
        )
        replacement.subscribe(USB_OBSERVATION_CONFIG_TOPIC, qos=1)

        previous = self.mqtt
        previous.publish(
            availability_topic(self.device_id),
            "offline",
            qos=1,
            retain=True,
        )
        previous.stop()
        self.mqtt = replacement
        if replacement.start():
            return True

        logger.error(
            "New MQTT configuration could not start; restoring previous client"
        )
        self.mqtt = previous
        previous.start()
        return False

    def _on_mqtt_message(self, topic: str, payload: str):
        """Fired instantly when an MQTT message arrives on subscribed topics."""
        logger.info(f"MQTT Message Received [{topic}]: {payload}")

        if topic == self.STREAMDECK_LAYOUT_TOPIC:
            if not self._ingest_streamdeck_layout(payload):
                logger.warning("Rejected invalid retained Stream Deck layout")
            return

        if topic == USB_OBSERVATION_CONFIG_TOPIC:
            try:
                config = USBSentinelConfig.model_validate_json(payload)
            except ValueError:
                logger.warning("Rejected invalid USB sentinel configuration")
                return
            with self._usb_observation_lock:
                self._usb_sentinel_config = config
                self._last_usb_observation = 0.0
            return

        if topic == kvm_hardware_command_topic(self.device_id):
            try:
                command = KVMHardwareCommand.model_validate_json(payload)
            except ValueError:
                logger.warning("Rejected invalid delegated KVM hardware command")
                return
            worker = threading.Thread(
                target=self._execute_kvm_hardware_command,
                args=(command,),
                name=f"kvm-hardware-{command.operation}",
                daemon=True,
            )
            worker.start()
            return

        target_set_topic = f"desk/{self.device_id}/audio/set"
        if topic == target_set_topic and self.audio_driver:
            target_device = payload.strip()
            logger.info(
                f"Received instant MQTT audio switch request: '{target_device}'"
            )
            success = self.audio_driver.set_output_device(target_device)
            if success:
                st = self.audio_driver.get_audio_state()
                act = st.active_device or target_device
                if not self.mqtt.publish(
                    f"desk/{self.device_id}/audio/state",
                    act,
                    retain=True,
                ):
                    logger.warning("Failed publishing confirmed audio state")
            return

        if topic == slot_command_topic(self.device_id):
            try:
                command = WorkstationSlotCommand.model_validate_json(payload)
            except ValueError:
                logger.warning("Rejected invalid workstation slot command")
                return
            if command.action == "activate":
                request_id = str(command.request_id) if command.request_id else None
                lock = getattr(self, "_slot_command_lock", threading.RLock())
                with lock:
                    cache = getattr(self, "_slot_result_cache", {})
                    result_payload = cache.get(request_id) if request_id else None
                    if result_payload is None:
                        success = self.workstation_buttons.activate(
                            command.slot_id,
                            self.audio_driver,
                            getattr(self, "media_driver", None),
                        )
                        if request_id:
                            result_payload = WorkstationSlotResult(
                                request_id=command.request_id,
                                slot_id=command.slot_id,
                                success=success,
                            ).model_dump(mode="json")
                            cache[request_id] = result_payload
                            while len(cache) > 256:
                                cache.pop(next(iter(cache)))
                            self._slot_result_cache = cache
                    else:
                        success = bool(result_payload["success"])

                if not success:
                    logger.warning(
                        "Could not activate workstation slot '%s'",
                        command.slot_id,
                    )
                self._publish_workstation_slots(force=True)
                if request_id and not self.mqtt.publish(
                    slot_result_topic(self.device_id),
                    result_payload,
                    qos=1,
                ):
                    logger.warning("Failed publishing workstation slot acknowledgement")

    def _execute_kvm_hardware_command(
        self,
        command: KVMHardwareCommand,
    ) -> None:
        """Execute or replay one idempotent locally authorized hardware step."""
        cache_key = str(command.command_id)
        command_lock = getattr(self, "_kvm_command_lock", self._kvm_result_lock)
        with command_lock:
            with self._kvm_result_lock:
                cached = self._kvm_result_cache.get(cache_key)
            if cached is None:
                success, input_source, detail = self.kvm_hardware.execute(command)
                result = KVMHardwareResult(
                    transaction_id=command.transaction_id,
                    command_id=command.command_id,
                    operation=command.operation,
                    success=success,
                    input_source=input_source,
                    detail=detail,
                ).model_dump(mode="json")
                with self._kvm_result_lock:
                    self._kvm_result_cache[cache_key] = result
                    while len(self._kvm_result_cache) > 64:
                        oldest = next(iter(self._kvm_result_cache))
                        self._kvm_result_cache.pop(oldest, None)
            else:
                result = cached

        if not self.mqtt.publish(
            kvm_hardware_result_topic(self.device_id),
            result,
            qos=1,
        ):
            logger.warning(
                "Could not publish delegated KVM result for %s",
                command.command_id,
            )

    def request_kvm_toggle(self) -> bool:
        """Ask the Pi coordinator to perform one complete KVM toggle."""
        request = KVMSwitchRequest(
            source_device_id=self.device_id,
            target="toggle",
        )
        success = self.mqtt.publish(
            KVM_REQUEST_TOPIC,
            request.model_dump(mode="json"),
            qos=1,
        )
        if success:
            logger.info("Global shortcut requested a coordinated KVM toggle")
        else:
            logger.warning("Could not publish the global KVM toggle request")
        return success

    def set_hotkey_reconfigure_callback(self, callback) -> None:
        self._hotkey_reconfigure_callback = callback

    def reconfigure_desktop_kvm(
        self,
        config: Mapping[str, Any],
    ) -> None:
        """Apply local delegated hardware and global-shortcut settings."""
        settings = DesktopKVMSettings.model_validate(config)
        self.kvm_hardware.reconfigure(settings)
        callback = self._hotkey_reconfigure_callback
        if callback is not None:
            callback(settings.hotkey)

    def _ingest_streamdeck_layout(self, payload: str) -> bool:
        """Cache the Pi-owned physical layout for the local button editor."""
        try:
            body = json.loads(payload)
        except json.JSONDecodeError:
            return False
        if not isinstance(body, dict):
            return False

        rows = body.get("rows")
        columns = body.get("columns")
        buttons = body.get("buttons")
        if (
            not isinstance(rows, int)
            or not 1 <= rows <= 8
            or not isinstance(columns, int)
            or not 1 <= columns <= 8
            or not isinstance(buttons, list)
            or len(buttons) > rows * columns
        ):
            return False

        filtered_buttons = []
        seen_keys = set()
        for raw_button in buttons:
            if not isinstance(raw_button, dict):
                return False
            key = raw_button.get("key")
            if (
                not isinstance(key, int)
                or not 0 <= key < rows * columns
                or key in seen_keys
            ):
                return False
            seen_keys.add(key)
            action_type = str(raw_button.get("action_type", "none"))
            button = {
                "key": key,
                "row": key // columns,
                "column": key % columns,
                "configured": bool(raw_button.get("configured", False)),
                "enabled": bool(raw_button.get("enabled", True)),
                "label": str(raw_button.get("label", ""))[:128],
                "icon": str(raw_button.get("icon", "none"))[:64],
                "action_type": action_type[:64],
            }
            if action_type == "workstation_slot":
                button["slot_id"] = str(raw_button.get("slot_id", ""))[:64]
            filtered_buttons.append(button)

        with self._layout_lock:
            self._streamdeck_layout = {
                "rows": rows,
                "columns": columns,
                "buttons": filtered_buttons,
            }
            self._layout_received = True
        return True

    def button_configuration_snapshot(self) -> Dict[str, Any]:
        """Return local button configuration plus the Pi-advertised layout."""
        with self._layout_lock:
            layout = deepcopy(self._streamdeck_layout)
            layout_received = self._layout_received
        kvm_hardware = getattr(self, "kvm_hardware", None)
        desktop_kvm = (
            kvm_hardware.settings if kvm_hardware is not None else DesktopKVMSettings()
        )
        return {
            "device_id": self.device_id,
            "hostname": self.hostname,
            "mqtt_connected": self.mqtt.is_connected,
            "mqtt_health": self.mqtt.connection_health(),
            "pi_sync_pending": (
                not self.mqtt.is_connected
                or getattr(self, "_last_slot_manifest", None) is None
            ),
            "layout_received": layout_received,
            "layout": layout,
            "slots": self.workstation_buttons.configured_slots(),
            "action_catalog": DESKTOP_ACTION_CATALOG,
            "desktop_kvm": desktop_kvm.model_dump(mode="json"),
        }

    def available_workstation_slot_ids(self) -> set:
        """Return enabled selected-computer keys advertised by the controller."""
        with self._layout_lock:
            buttons = list(self._streamdeck_layout.get("buttons", []))
        return {
            str(button.get("slot_id", "")).strip()
            for button in buttons
            if button.get("enabled", True)
            and button.get("action_type") == "workstation_slot"
            and str(button.get("slot_id", "")).strip()
        }

    def reconfigure_workstation_buttons(
        self,
        config: Mapping[str, Mapping[str, Any]],
    ) -> None:
        """Apply validated local actions and immediately advertise them."""
        replacement = DesktopWorkstationButtonRegistry.from_config(
            config,
            self.os_type,
        )
        self.workstation_buttons = replacement
        self._last_slot_manifest = None
        self._last_slot_state = None
        self._publish_workstation_slots(force=True)

    def _publish_workstation_slots(self, force: bool = False) -> None:
        registry = getattr(self, "workstation_buttons", None)
        if registry is None:
            return

        media = (
            self.media_driver.get_now_playing()
            if getattr(self, "media_driver", None)
            else NowPlayingState()
        )
        manifest = WorkstationSlotManifest(
            device_id=self.device_id,
            slots=registry.manifest_slots(media=media),
        )
        manifest_payload = manifest.model_dump(mode="json")
        if force or manifest_payload != getattr(self, "_last_slot_manifest", None):
            if not self.mqtt.publish(
                slot_manifest_topic(self.device_id),
                manifest_payload,
                qos=1,
                retain=True,
            ):
                logger.warning("Failed publishing workstation slot manifest")
            else:
                self._last_slot_manifest = manifest_payload

        audio_muted = False
        if self.audio_driver:
            audio_muted = self.audio_driver.get_audio_state().is_muted
        active = registry.active_state(
            audio_muted=audio_muted,
            media=media,
        )
        if not force and active == getattr(self, "_last_slot_state", None):
            return
        state = WorkstationSlotState(
            device_id=self.device_id,
            active=active,
        )
        if self.mqtt.publish(
            slot_state_topic(self.device_id),
            state.model_dump(mode="json"),
            qos=1,
            retain=True,
        ):
            self._last_slot_state = active
        else:
            logger.warning("Failed publishing workstation slot state")

    def register_ha_discovery(self):
        """Sends Home Assistant MQTT Discovery payloads for automatic device registration."""
        logger.info(
            f"Registering Home Assistant MQTT Discovery for '{self.hostname}'..."
        )

        audio_state = self.audio_driver.get_audio_state() if self.audio_driver else None
        audio_options = (
            [device.name for device in audio_state.available_devices]
            if audio_state
            else []
        )

        device_info = {
            "identifiers": [f"desk_agent_{self.device_id}"],
            "name": f"{self.hostname} Workstation",
            "model": f"Desktop Agent ({self.os_type.capitalize()})",
            "manufacturer": "Antigravity Smart Desk",
        }

        # 1. Audio Output Select Entity in HA
        audio_select_config = {
            "name": f"{self.hostname} Audio Output",
            "unique_id": f"{self.device_id}_audio_output_select",
            "command_topic": f"desk/{self.device_id}/audio/set",
            "state_topic": f"desk/{self.device_id}/audio/state",
            "options": audio_options,
            "icon": "mdi:speaker",
            "device": device_info,
        }
        audio_discovery_ok = self.mqtt.publish(
            f"homeassistant/select/{self.device_id}_audio/config",
            audio_select_config,
            retain=True,
        )

        # 2. CPU Usage Sensor Entity in HA
        cpu_sensor_config = {
            "name": f"{self.hostname} CPU Usage",
            "unique_id": f"{self.device_id}_cpu_sensor",
            "state_topic": f"desk/{self.device_id}/telemetry",
            "value_template": "{{ value_json.cpu_percent }}",
            "unit_of_measurement": "%",
            "icon": "mdi:cpu-64-bit",
            "device": device_info,
        }
        cpu_discovery_ok = self.mqtt.publish(
            f"homeassistant/sensor/{self.device_id}_cpu/config",
            cpu_sensor_config,
            retain=True,
        )
        if not audio_discovery_ok or not cpu_discovery_ok:
            logger.warning("One or more Home Assistant discovery publishes failed")

    def publish_state(self):
        """Publishes current CPU, RAM, and active audio state over MQTT."""
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        audio_st = self.audio_driver.get_audio_state() if self.audio_driver else None
        active_audio = audio_st.active_device if audio_st else None

        telemetry_payload = TelemetryMetrics(
            device_id=self.device_id,
            hostname=self.hostname,
            os_type=self.os_type,
            cpu_percent=cpu,
            memory_percent=mem,
            lan_ip=(
                self.lan_ip
                if hasattr(self, "lan_ip")
                else get_lan_ip(self.mqtt.broker, self.mqtt.port)
            ),
            active_audio=active_audio,
        ).model_dump(mode="json")
        if not self.mqtt.publish(
            f"desk/{self.device_id}/telemetry",
            telemetry_payload,
        ):
            logger.warning("Failed publishing workstation telemetry")

        if active_audio:
            if not self.mqtt.publish(
                f"desk/{self.device_id}/audio/state",
                active_audio,
                retain=True,
            ):
                logger.warning("Failed publishing active audio state")
        self._publish_workstation_slots()
        self._publish_usb_observation()

    def _publish_usb_observation(self, force: bool = False) -> bool:
        """Publish whether this workstation currently sees the sentinel."""
        lock = getattr(self, "_usb_observation_lock", None)
        config = getattr(self, "_usb_sentinel_config", None)
        probe = getattr(self, "_usb_presence_probe", None)
        if lock is None or config is None or probe is None:
            return False
        with lock:
            if not config.is_complete():
                return False
            now = time.monotonic()
            if not force and now - self._last_usb_observation < config.poll_interval:
                return False
            self._last_usb_observation = now
            present = probe.is_present(config)
            observation = USBPresenceObservation(
                device_id=self.device_id,
                vendor_id=config.vendor_id,
                product_id=config.product_id,
                serial_number=config.serial_number,
                available=present is not None,
                present=bool(present),
            )
        return self.mqtt.publish(
            usb_observation_topic(self.device_id),
            observation.model_dump(mode="json"),
            qos=1,
            retain=True,
        )

    def run(self):
        logger.info(
            f"Starting MQTT Desktop Agent on {self.hostname} ({self.os_type})..."
        )
        self.mqtt.subscribe(f"desk/{self.device_id}/audio/set")
        self.mqtt.subscribe(slot_command_topic(self.device_id), qos=1)
        self.mqtt.subscribe(self.STREAMDECK_LAYOUT_TOPIC, qos=1)
        self.mqtt.subscribe(
            kvm_hardware_command_topic(self.device_id),
            qos=1,
        )
        self.mqtt.subscribe(USB_OBSERVATION_CONFIG_TOPIC, qos=1)
        if not self.mqtt.start():
            logger.error("MQTT network loop failed to start")
        elif not self.mqtt.wait_until_connected(timeout=10):
            logger.warning(
                "MQTT did not connect within 10 seconds; retrying in background"
            )

        wake_event = getattr(self, "_poll_wake_event", threading.Event())
        while not self._stop_event.is_set():
            wake_event.clear()
            connected = bool(self.mqtt.is_connected)
            if connected:
                try:
                    self.publish_state()
                except Exception as e:
                    logger.error(f"Error in agent main loop: {e}")
            poll_interval = (
                self.ONLINE_POLL_INTERVAL if connected else self.OFFLINE_POLL_INTERVAL
            )
            wake_event.wait(poll_interval)

    def stop(self) -> bool:
        """Publish graceful offline state and stop the background agent loop."""
        self._stop_event.set()
        wake_event = getattr(self, "_poll_wake_event", None)
        if wake_event is not None:
            wake_event.set()
        kvm_hardware = getattr(self, "kvm_hardware", None)
        if kvm_hardware is not None:
            kvm_hardware.close()
        self.mqtt.publish(
            availability_topic(self.device_id),
            "offline",
            qos=1,
            retain=True,
        )
        return self.mqtt.stop()


if __name__ == "__main__":
    agent = DesktopAgent()
    agent.run()
