"""
Main entry point for Raspberry Pi 4 Smart Desk & KVM Controller (MQTT Native).
"""

import json
import logging
import math
import platform
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional
from uuid import UUID, uuid4

import uvicorn

from desk_controller import __version__
from desk_controller.config import load_config, resolve_config_path
from desk_controller.core.kvm import (
    KVM_REQUEST_TOPIC,
    KVMHardwareCommand,
    KVMHardwareResult,
    KVMSwitchRequest,
    kvm_hardware_command_topic,
    kvm_hardware_result_topic,
    parse_kvm_hardware_result_topic,
)
from desk_controller.core.models import DeviceStatus
from desk_controller.core.mqtt_client import MQTTClientHelper
from desk_controller.core.network import get_lan_ip
from desk_controller.core.pending_requests import PendingWorkstationRequests
from desk_controller.core.usb_observation import (
    USB_OBSERVATION_CONFIG_TOPIC,
    USBPresenceObservation,
    USBSentinelConfig,
    parse_usb_observation_topic,
    usb_observation_topic,
)
from desk_controller.core.workstation_slots import (
    WorkstationSlotCommand,
    WorkstationSlotResult,
    parse_slot_result_topic,
    parse_workstation_topic,
    slot_command_topic,
    slot_result_topic,
)
from desk_controller.pi_controller.api.app import (
    app,
    configure_command_publisher,
    configure_usb_hub_api,
    ingest_mqtt_telemetry,
)
from desk_controller.pi_controller.api.config_ui import configure_config_ui
from desk_controller.pi_controller.drivers.monitor_ddc import MonitorDDCController
from desk_controller.pi_controller.drivers.streamdeck_mgr import StreamDeckManager
from desk_controller.pi_controller.drivers.usb_switch import (
    create_usb_controller,
)
from desk_controller.pi_controller.integrations.homeassistant import HomeAssistantClient
from desk_controller.pi_controller.streamdeck_layout import (
    DYNAMIC_DATETIME_ACTIONS,
    STREAMDECK_KEY_COUNT,
    configured_streamdeck_buttons,
    configured_usb_ports,
    datetime_button_label,
)
from desk_controller.pi_controller.workstation_deck import (
    SelectedWorkstationDeck,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("MainController")


class DeskControllerApp:
    DEVICE_ID = "rpi_desk_controller"
    STREAMDECK_TOPIC = "desk/rpi_desk_controller/streamdeck"
    USB_HUB_TOPIC = "desk/rpi_desk_controller/usb_hub"

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = resolve_config_path(config_path)
        self.config = load_config(str(self.config_path))
        self._stop_event = threading.Event()
        acroname_conf = self.config.get("acroname", {})
        usb_switch_conf = self.config.get("usb_switch", {})
        monitors_conf = self.config.get("monitors", [{}])
        monitor_conf = monitors_conf[0] if monitors_conf else {}
        simulate_hardware = self.config.get("hardware", {}).get("simulate", False)
        # ``acroname`` remains as a compatibility alias because older
        # integrations and tests access that attribute directly.
        self.acroname = create_usb_controller(
            self.config,
            simulate=simulate_hardware,
        )
        self.monitor = MonitorDDCController(
            display_id=monitor_conf.get("display_id", 1),
            simulate=simulate_hardware,
        )

        ha_conf = self.config.get("homeassistant", {})
        self.homeassistant_enabled = bool(ha_conf.get("enabled", True))
        self.ha = HomeAssistantClient(
            base_url=ha_conf.get("url", "http://homeassistant.local:8123"),
            token=ha_conf.get("token", ""),
        )

        sd_brightness = self.config.get("streamdeck", {}).get("brightness", 85)
        streamdeck_conf = self.config.get("streamdeck", {})
        try:
            pending_timeout = float(streamdeck_conf.get("pending_request_timeout", 300))
        except (TypeError, ValueError):
            pending_timeout = 300.0
        if not math.isfinite(pending_timeout) or pending_timeout <= 0:
            pending_timeout = 300.0
        try:
            pending_retry_interval = float(
                streamdeck_conf.get("pending_retry_interval", 5)
            )
        except (TypeError, ValueError):
            pending_retry_interval = 5.0
        if not math.isfinite(pending_retry_interval) or pending_retry_interval <= 0:
            pending_retry_interval = 5.0
        self._pending_workstation_requests = PendingWorkstationRequests(
            timeout=pending_timeout,
            retry_interval=pending_retry_interval,
        )
        self.streamdeck = StreamDeckManager(
            key_callback=self._handle_key_press, brightness=sd_brightness
        )
        self.physical_buttons = configured_streamdeck_buttons(self.config)
        self.buttons = dict(self.physical_buttons)
        self.usb_ports = {
            index: values["name"]
            for index, values in configured_usb_ports(self.config).items()
        }
        usb_hub_conf = self.config.get("usb_hub", {})
        self._hub_telemetry_interval = max(
            2,
            int(usb_hub_conf.get("telemetry_interval", 10)),
        )
        self._last_hub_telemetry = 0.0
        self._lan_ip = ""
        self.current_pc = int(
            usb_switch_conf.get(
                "default_channel",
                acroname_conf.get("default_channel", 0),
            )
        )
        if self.current_pc not in (0, 1):
            raise ValueError("usb_switch.default_channel must be 0 or 1")
        self._kvm_lock = threading.RLock()
        self._remote_kvm_lock = threading.RLock()
        self._remote_kvm_waiters: Dict[str, Dict[str, Any]] = {}
        self._kvm_request_ids: Dict[str, float] = {}
        self._kvm_worker: Optional[threading.Thread] = None
        self._usb_observation_condition = threading.Condition(threading.RLock())
        self._usb_observations: Dict[str, Dict[str, Any]] = {}
        self.workstation_deck = SelectedWorkstationDeck(self.physical_buttons)
        self.buttons = self.workstation_deck.resolve(self._get_active_hostname())
        self.kvm_fault = False
        self.active_audio_device = ""
        self._audio_state_by_host: Dict[str, str] = {}
        self.active_groups: Dict[str, int] = {}
        self._clock_display_minute: Optional[str] = None

        # Setup MQTT Client with HA credentials
        mqtt_conf = self.config.get("mqtt", {})
        mqtt_mode = str(mqtt_conf.get("mode", "external")).strip().lower()
        broker_ip = (
            "127.0.0.1"
            if mqtt_mode == "local"
            else mqtt_conf.get("broker", "homeassistant.local")
        )
        port = mqtt_conf.get("port", 1883)
        username = mqtt_conf.get("username", "")
        password = mqtt_conf.get("password", "")

        self.mqtt = MQTTClientHelper(
            client_id="rpi_desk_controller",
            broker=broker_ip,
            port=port,
            username=username,
            password=password,
            on_message_callback=self._on_mqtt_message,
            on_connect_callback=self._on_mqtt_connected,
        )
        configure_command_publisher(self.mqtt.publish)
        configure_usb_hub_api(
            self._get_usb_hub_status,
            self._control_usb_hub_port,
            self._control_usb_hub,
        )
        configure_config_ui(
            self.config_path,
            restart_callback=self._request_restart,
            connection_status_provider=self._connection_status,
        )

    def _connection_status(self) -> Dict[str, Any]:
        """Return current, secret-free connection and authentication health."""
        mqtt_conf = self.config.get("mqtt", {})
        mqtt_mode = str(mqtt_conf.get("mode", "external")).strip().lower()
        mqtt_connected = self.mqtt.is_connected
        health_provider = getattr(self.mqtt, "connection_health", None)
        mqtt_health = (
            health_provider()
            if callable(health_provider)
            else {
                "disconnected_seconds": None,
                "last_disconnect_reason": None,
                "recovery_count": 0,
            }
        )
        mqtt_username = str(mqtt_conf.get("username", "")).strip()
        if mqtt_connected and mqtt_username:
            mqtt_detail = f"Broker accepted credentials for {mqtt_username}."
        elif mqtt_connected:
            mqtt_detail = "Connected without a username."
        else:
            mqtt_detail = "The controller is not connected to the broker."

        if self.homeassistant_enabled:
            homeassistant_status = self.ha.connection_status()
            homeassistant_status["enabled"] = True
            homeassistant_status["url"] = self.ha.base_url
        else:
            homeassistant_status = {
                "enabled": False,
                "status": "disabled",
                "authenticated": False,
                "http_status": None,
                "url": self.ha.base_url,
                "detail": "Home Assistant integration is disabled.",
            }

        return {
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "mqtt": {
                "status": ("connected" if mqtt_connected else "disconnected"),
                "connected": mqtt_connected,
                "mode": mqtt_mode,
                "broker": self.mqtt.broker,
                "port": self.mqtt.port,
                "detail": mqtt_detail,
                "disconnected_seconds": mqtt_health.get("disconnected_seconds"),
                "last_disconnect_reason": mqtt_health.get("last_disconnect_reason"),
                "recovery_count": mqtt_health.get("recovery_count", 0),
            },
            "homeassistant": homeassistant_status,
        }

    def _request_restart(self):
        """Ask the main loop to shut down cleanly for systemd to restart it."""
        logger.info("Controller restart requested from configuration UI")
        timer = threading.Timer(0.75, self._stop_event.set)
        timer.daemon = True
        timer.start()

    def _on_mqtt_connected(self):
        """Restore retained discovery and KVM state after every connection."""
        self._lan_ip = get_lan_ip(self.mqtt.broker, self.mqtt.port)
        if getattr(self, "homeassistant_enabled", True):
            self.register_ha_kvm_discovery()
            self.register_ha_streamdeck_discovery()
            self.register_ha_usb_hub_discovery()
        status = DeviceStatus(
            device_id=self.DEVICE_ID,
            hostname=platform.node(),
            os_type="linux",
            lan_ip=self._lan_ip,
            app_version=__version__,
        ).model_dump(mode="json")
        if not self.mqtt.publish(
            f"desk/{self.DEVICE_ID}/status",
            status,
            retain=True,
        ):
            logger.warning("Failed publishing retained controller status")
        with self._kvm_lock:
            current_pc = self.current_pc
            kvm_fault = self.kvm_fault
        self.mqtt.publish(
            "desk/kvm/state",
            f"PC{current_pc + 1}",
            retain=True,
        )
        self.mqtt.publish(
            "desk/kvm/availability",
            "offline" if kvm_fault else "online",
            retain=True,
        )
        self._publish_streamdeck_layout()
        self._publish_streamdeck_state()
        self._publish_usb_observation_config()
        self._publish_usb_hub_config()
        self._publish_usb_hub_state()

    def _get_active_hostname(self) -> str:
        """Returns the active workstation device_id based on KVM channel."""
        workstations = self.config.get(
            "workstations",
            {"pc1": "computer-one", "pc2": "computer-two"},
        )
        return workstations.get(
            f"pc{self.current_pc + 1}",
            "computer-one" if self.current_pc == 0 else "computer-two",
        )

    def _configured_workstation_ids(self) -> set:
        workstations = self.config.get("workstations", {})
        return {
            str(device_id).strip()
            for device_id in workstations.values()
            if str(device_id).strip()
        }

    def _resolve_kvm_controller(
        self,
        component: str,
        active_pc: Optional[int] = None,
    ) -> str:
        """Resolve a component owner to ``pi`` or an exact workstation ID."""
        if component not in {"monitor", "usb"}:
            raise ValueError("KVM component must be monitor or usb")
        owner = str(
            self.config.get("kvm", {}).get(
                f"{component}_controller",
                "pi",
            )
        ).strip()
        if not owner or owner == "pi":
            return "pi"
        if active_pc is None:
            active_pc = self.current_pc
        workstations = self.config.get("workstations", {})
        if owner == "active_workstation":
            owner = f"pc{active_pc + 1}"
        if owner in {"pc1", "pc2"}:
            return str(workstations.get(owner, "")).strip()
        if owner in self._configured_workstation_ids():
            return owner
        logger.error("Invalid %s KVM controller owner: %s", component, owner)
        return ""

    def _pi_controls_usb(self) -> bool:
        return self._resolve_kvm_controller("usb") == "pi"

    def _usb_sentinel_config(self) -> USBSentinelConfig:
        values = self.config.get("usb_switch", {})
        if not isinstance(values, dict):
            values = {}
        enabled = str(
            values.get("driver", "acroname")
        ).strip().lower() == "ugreen_cm691_gpio" and bool(
            values.get("observation_enabled", False)
        )
        return USBSentinelConfig(
            enabled=enabled,
            vendor_id=values.get("sentinel_vendor_id"),
            product_id=values.get("sentinel_product_id"),
            serial_number=values.get("sentinel_serial_number"),
            poll_interval=values.get("observation_poll_interval", 2),
        )

    def _publish_usb_observation_config(self) -> bool:
        config = self._usb_sentinel_config()
        return self.mqtt.publish(
            USB_OBSERVATION_CONFIG_TOPIC,
            config.model_dump(mode="json"),
            qos=1,
            retain=True,
        )

    def _ingest_usb_observation(
        self,
        device_id: str,
        payload: str,
    ) -> bool:
        if device_id not in self._configured_workstation_ids():
            return False
        try:
            observation = USBPresenceObservation.model_validate_json(payload)
        except ValueError:
            return False
        config = self._usb_sentinel_config()
        if (
            observation.device_id != device_id
            or not config.is_complete()
            or not config.matches(observation)
        ):
            return False
        condition = getattr(self, "_usb_observation_condition", None)
        if condition is None:
            return False
        with condition:
            self._usb_observations[device_id] = {
                "observation": observation,
                "received_at": time.monotonic(),
            }
            condition.notify_all()
        if (
            getattr(self, "kvm_fault", False)
            and self._observed_usb_channel() is not None
        ):
            self._start_kvm_reconcile()
        return True

    def _observed_usb_channel(self) -> Optional[int]:
        """Return the one workstation that freshly reports the sentinel."""
        config = self._usb_sentinel_config()
        if not config.is_complete():
            return None
        timeout = float(
            self.config.get("usb_switch", {}).get(
                "observation_timeout",
                8,
            )
        )
        max_age = max(timeout, config.poll_interval * 3)
        now_monotonic = time.monotonic()
        now_utc = datetime.now(timezone.utc)
        workstations = self.config.get("workstations", {})
        present_channels = []
        condition = getattr(self, "_usb_observation_condition", None)
        if condition is None:
            return None
        with condition:
            for channel in (0, 1):
                device_id = str(workstations.get(f"pc{channel + 1}", "")).strip()
                item = self._usb_observations.get(device_id)
                if not device_id or item is None:
                    continue
                observation = item["observation"]
                received_age = now_monotonic - item["received_at"]
                observed_age = (
                    now_utc - observation.observed_at.astimezone(timezone.utc)
                ).total_seconds()
                workstation_deck = getattr(self, "workstation_deck", None)
                online = workstation_deck is None or workstation_deck.selected_state(
                    device_id
                ).get(
                    "agent_online",
                    False,
                )
                if (
                    online
                    and observation.available
                    and received_age <= max_age
                    and -60 <= observed_age <= max_age
                    and observation.present
                ):
                    present_channels.append(channel)
        return present_channels[0] if len(present_channels) == 1 else None

    def _wait_for_observed_usb_channel(
        self,
        target: Optional[int] = None,
    ) -> Optional[int]:
        timeout = float(
            self.config.get("usb_switch", {}).get(
                "observation_timeout",
                8,
            )
        )
        deadline = time.monotonic() + max(3.0, min(timeout, 60.0))
        condition = self._usb_observation_condition
        with condition:
            while True:
                observed = self._observed_usb_channel()
                if observed is not None and (target is None or observed == target):
                    return observed
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                condition.wait(min(remaining, 1.0))

    def _ingest_remote_kvm_result(
        self,
        device_id: str,
        payload: str,
    ) -> bool:
        try:
            result = KVMHardwareResult.model_validate_json(payload)
        except ValueError:
            return False
        command_id = str(result.command_id)
        with self._remote_kvm_lock:
            waiter = self._remote_kvm_waiters.get(command_id)
            if (
                waiter is None
                or waiter["device_id"] != device_id
                or waiter["transaction_id"] != result.transaction_id
                or waiter["operation"] != result.operation
            ):
                return False
            waiter["result"] = result
            waiter["event"].set()
        return True

    def _accept_kvm_request(self, request_id: UUID) -> bool:
        """Accept a workstation request once despite MQTT QoS 1 redelivery."""
        now = time.monotonic()
        request_ids = getattr(self, "_kvm_request_ids", None)
        if request_ids is None:
            request_ids = {}
            self._kvm_request_ids = request_ids
        with self._remote_kvm_lock:
            for cached_id, received_at in list(request_ids.items()):
                if now - received_at > 300:
                    request_ids.pop(cached_id, None)
            key = str(request_id)
            if key in request_ids:
                return False
            request_ids[key] = now
            while len(request_ids) > 128:
                request_ids.pop(next(iter(request_ids)), None)
        return True

    def _execute_remote_kvm_step(
        self,
        device_id: str,
        transaction_id: UUID,
        operation: str,
        target_pc: Optional[int] = None,
    ) -> Optional[KVMHardwareResult]:
        if not device_id:
            return None
        command = KVMHardwareCommand(
            transaction_id=transaction_id,
            operation=operation,
            target_pc=target_pc,
        )
        command_id = str(command.command_id)
        waiter = {
            "device_id": device_id,
            "transaction_id": transaction_id,
            "operation": operation,
            "event": threading.Event(),
            "result": None,
        }
        with self._remote_kvm_lock:
            self._remote_kvm_waiters[command_id] = waiter
        try:
            if not self.mqtt.publish(
                kvm_hardware_command_topic(device_id),
                command.model_dump(mode="json"),
                qos=1,
            ):
                return None
            timeout = float(self.config.get("kvm", {}).get("remote_timeout", 10))
            if not waiter["event"].wait(max(2.0, min(timeout, 30.0))):
                logger.error(
                    "Timed out waiting for %s KVM step from '%s'",
                    operation,
                    device_id,
                )
                return None
            return waiter["result"]
        finally:
            with self._remote_kvm_lock:
                self._remote_kvm_waiters.pop(command_id, None)

    def _kvm_usb_set(
        self,
        owner: str,
        target_pc: int,
        transaction_id: UUID,
    ) -> bool:
        if owner == "pi":
            sentinel = self._usb_sentinel_config()
            verified_toggle = (
                getattr(self.acroname, "DRIVER", "") == "ugreen_cm691_gpio"
                and sentinel.is_complete()
            )
            if not verified_toggle:
                return self.acroname.switch_upstream_channel(target_pc)

            observed = self._wait_for_observed_usb_channel()
            if observed is None:
                logger.error(
                    "Cannot target the UGREEN switch: USB sentinel state is unknown"
                )
                return False
            self.acroname.active_channel = observed
            if observed == target_pc:
                return True
            if not self.acroname.switch_upstream_channel(target_pc):
                return False
            if self._wait_for_observed_usb_channel(target_pc) is None:
                logger.error(
                    "UGREEN switched but PC%s did not observe the USB sentinel",
                    target_pc + 1,
                )
                return False
            return True
        result = self._execute_remote_kvm_step(
            owner,
            transaction_id,
            "usb_set",
            target_pc,
        )
        if result is not None and not result.success:
            logger.error("Remote USB switch failed: %s", result.detail)
        return bool(result and result.success)

    def _kvm_monitor_get(
        self,
        owner: str,
        transaction_id: UUID,
    ) -> Optional[int]:
        if owner == "pi":
            return self.monitor.get_input_source()
        result = self._execute_remote_kvm_step(
            owner,
            transaction_id,
            "monitor_get",
        )
        if result is None or not result.success:
            if result is not None:
                logger.error("Remote monitor read failed: %s", result.detail)
            return None
        return result.input_source

    def _kvm_monitor_set(
        self,
        owner: str,
        target_pc: int,
        transaction_id: UUID,
    ) -> bool:
        if owner == "pi":
            monitors_cfg = self.config.get("monitors", [{}])
            inputs_cfg = (monitors_cfg[0] if monitors_cfg else {}).get("inputs", {})
            input_code = inputs_cfg.get(
                f"pc{target_pc + 1}",
                "0x0f" if target_pc == 0 else "0x11",
            )
            return self.monitor.set_input_source(input_code)
        result = self._execute_remote_kvm_step(
            owner,
            transaction_id,
            "monitor_set",
            target_pc,
        )
        if result is not None and not result.success:
            logger.error("Remote monitor switch failed: %s", result.detail)
        return bool(result and result.success)

    def _start_kvm_switch(self, target_pc: int) -> bool:
        """Start a non-blocking switch when called from the MQTT network loop."""
        worker = getattr(self, "_kvm_worker", None)
        if worker is not None and worker.is_alive():
            logger.warning("Ignoring KVM request while a switch is already running")
            return False
        self._kvm_worker = threading.Thread(
            target=self._switch_kvm,
            args=(target_pc,),
            name="kvm-transaction",
            daemon=True,
        )
        self._kvm_worker.start()
        return True

    def _reconcile_kvm_state(self) -> bool:
        """Re-read ownership state and align USB without changing the monitor."""
        with self._kvm_lock:
            success = self._initialize_kvm_state()
            self._apply_active_audio_state()
            self.mqtt.publish(
                "desk/kvm/state",
                f"PC{self.current_pc + 1}",
                retain=True,
            )
            self.mqtt.publish(
                "desk/kvm/availability",
                "online" if success else "offline",
                retain=True,
            )
            self._update_sd_keys()
            self._publish_streamdeck_layout()
            self._publish_streamdeck_state()
            return success

    def _start_kvm_reconcile(self) -> bool:
        worker = getattr(self, "_kvm_worker", None)
        if worker is not None and worker.is_alive():
            return False
        self._kvm_worker = threading.Thread(
            target=self._reconcile_kvm_state,
            name="kvm-reconcile",
            daemon=True,
        )
        self._kvm_worker.start()
        return True

    def _selected_agent_online(self) -> bool:
        """Return whether the agent for the committed KVM host is online."""
        workstation_deck = getattr(self, "workstation_deck", None)
        if workstation_deck is None:
            return False
        return bool(
            workstation_deck.selected_state(self._get_active_hostname()).get(
                "agent_online"
            )
        )

    def _workstation_agent_online(self, device_id: str) -> bool:
        workstation_deck = getattr(self, "workstation_deck", None)
        if workstation_deck is None:
            return False
        return bool(workstation_deck.selected_state(device_id).get("agent_online"))

    def _queue_workstation_request(
        self,
        key: int,
        device_id: str,
        action_type: str,
        target: str,
    ) -> bool:
        pending = getattr(self, "_pending_workstation_requests", None)
        if pending is None:
            return False
        request = pending.add(
            key,
            device_id,
            action_type,
            target,
            replace_action_type=action_type == "audio_output",
        )
        logger.info(
            "Queued workstation request %s for '%s' key %s",
            request.request_id,
            device_id,
            key,
        )
        self._process_pending_workstation_requests(
            device_id=device_id,
            force=True,
        )
        return True

    def _process_pending_workstation_requests(
        self,
        device_id: Optional[str] = None,
        *,
        force: bool = False,
    ) -> bool:
        """Retry deliverable requests and expire requests that are no longer tried."""
        pending = getattr(self, "_pending_workstation_requests", None)
        if pending is None:
            return False

        changed = pending.expire()
        requests = pending.snapshot()
        device_ids = {
            request.device_id
            for request in requests
            if device_id is None or request.device_id == device_id
        }
        for pending_device_id in device_ids:
            if not self._workstation_agent_online(pending_device_id):
                continue
            for request in pending.due(
                pending_device_id,
                force=force and pending_device_id == device_id,
            ):
                if request.action_type == "audio_output":
                    published = self.mqtt.publish(
                        f"desk/{request.device_id}/audio/set",
                        request.target,
                        qos=1,
                    )
                elif request.action_type == "workstation_slot":
                    command = WorkstationSlotCommand(
                        request_id=request.request_id,
                        slot_id=request.target,
                    )
                    published = self.mqtt.publish(
                        slot_command_topic(request.device_id),
                        command.model_dump(mode="json"),
                        qos=1,
                    )
                else:
                    published = False
                if not published:
                    logger.warning(
                        "Pending workstation request %s attempt %s was not published",
                        request.request_id,
                        request.attempts,
                    )
        return changed

    def _acknowledge_audio_request(
        self,
        device_id: str,
        active_target: str,
    ) -> bool:
        pending = getattr(self, "_pending_workstation_requests", None)
        return bool(
            pending
            and pending.acknowledge_audio(
                device_id,
                active_target,
                self._audio_names_match,
            )
        )

    def _key_has_pending_request(
        self,
        device_id: str,
        key: int,
    ) -> bool:
        pending = getattr(self, "_pending_workstation_requests", None)
        return bool(pending and pending.is_pending(device_id, key))

    @staticmethod
    def _audio_state_host(topic: str) -> Optional[str]:
        """Extract a workstation ID from an exact desk audio state topic."""
        parts = topic.split("/")
        if (
            len(parts) == 4
            and parts[0] == "desk"
            and parts[1]
            and parts[2:] == ["audio", "state"]
        ):
            return parts[1]
        return None

    def _apply_active_audio_state(self) -> None:
        """Restore the cached audio selection for the committed KVM host."""
        active_host = self._get_active_hostname()
        self.active_audio_device = self._audio_state_by_host.get(
            active_host,
            "",
        )

        audio_groups = {
            str(button.get("group", "")).strip()
            for button in self.buttons.values()
            if button.get("enabled", True)
            and button.get("action_type") == "audio_output"
            and str(button.get("group", "")).strip()
        }
        for group in audio_groups:
            self.active_groups.pop(group, None)

        for key, button in self.buttons.items():
            if (
                self.active_audio_device
                and button.get("enabled", True)
                and button.get("action_type") == "audio_output"
                and self._audio_names_match(
                    self.active_audio_device,
                    str(button.get("target", "")),
                )
            ):
                group = str(button.get("group", "")).strip()
                if group:
                    self.active_groups[group] = key
                break

        logger.info(
            "Active audio state for workstation '%s': %s",
            active_host,
            self.active_audio_device or "unknown",
        )

    def _refresh_workstation_deck(self) -> None:
        """Atomically replace the resolved deck for the committed KVM host."""
        workstation_deck = getattr(self, "workstation_deck", None)
        if workstation_deck is None:
            return
        self.buttons = workstation_deck.resolve(self._get_active_hostname())

    def _on_mqtt_message(self, topic: str, payload: str):
        """Fired when an MQTT message arrives on subscribed topics."""
        logger.info(f"MQTT Message Received [{topic}]: {payload}")

        if (
            getattr(self, "homeassistant_enabled", True)
            and topic == "homeassistant/status"
        ):
            if payload.strip().lower() == "online":
                self.register_ha_kvm_discovery()
                self.register_ha_streamdeck_discovery()
                self.register_ha_usb_hub_discovery()
            return

        if (observation_device_id := parse_usb_observation_topic(topic)) is not None:
            if not self._ingest_usb_observation(
                observation_device_id,
                payload,
            ):
                logger.warning(
                    "Rejected USB sentinel observation on '%s'",
                    topic,
                )
            return

        if (remote_device_id := parse_kvm_hardware_result_topic(topic)) is not None:
            if (
                remote_device_id not in self._configured_workstation_ids()
                or not self._ingest_remote_kvm_result(
                    remote_device_id,
                    payload,
                )
            ):
                logger.warning(
                    "Rejected unexpected remote KVM result on '%s'",
                    topic,
                )
            return

        if topic == KVM_REQUEST_TOPIC:
            try:
                request = KVMSwitchRequest.model_validate_json(payload)
            except ValueError:
                logger.warning("Rejected invalid workstation KVM request")
                return
            if request.source_device_id not in self._configured_workstation_ids():
                logger.warning(
                    "Rejected KVM request from unconfigured workstation '%s'",
                    request.source_device_id,
                )
                return
            if not self._accept_kvm_request(request.request_id):
                logger.info(
                    "Ignored duplicate KVM request %s",
                    request.request_id,
                )
                return
            target_pc = {
                "toggle": 1 - self.current_pc,
                "pc1": 0,
                "pc2": 1,
            }[request.target]
            self._start_kvm_switch(target_pc)
            return

        workstation_topic = parse_workstation_topic(topic)
        if (
            workstation_topic is not None
            and workstation_topic[0] in self._configured_workstation_ids()
            and workstation_topic[1] in {"availability", "manifest", "state"}
        ):
            workstation_deck = getattr(self, "workstation_deck", None)
            updated_host = (
                workstation_deck.ingest(topic, payload)
                if workstation_deck is not None
                else None
            )
            if updated_host is None:
                logger.warning(
                    "Rejected invalid workstation deck message on '%s'",
                    topic,
                )
            elif updated_host == self._get_active_hostname():
                self._refresh_workstation_deck()
                self._update_sd_keys()
                self._publish_streamdeck_layout()
                self._publish_streamdeck_state()
            if (
                workstation_topic[1] == "availability"
                and payload.strip().lower() == "online"
                and updated_host
            ):
                self._process_pending_workstation_requests(
                    device_id=updated_host,
                    force=True,
                )
            if (
                workstation_topic[1] == "availability"
                and payload.strip().lower() == "online"
                and self.kvm_fault
                and updated_host
                in {
                    self._resolve_kvm_controller("monitor"),
                    self._resolve_kvm_controller("usb"),
                }
            ):
                self._start_kvm_reconcile()
            return

        slot_result_host = parse_slot_result_topic(topic)
        if (
            slot_result_host is not None
            and slot_result_host in self._configured_workstation_ids()
        ):
            try:
                result = WorkstationSlotResult.model_validate_json(payload)
            except ValueError:
                logger.warning(
                    "Rejected invalid workstation slot result on '%s'",
                    topic,
                )
                return
            pending = getattr(self, "_pending_workstation_requests", None)
            if pending and pending.acknowledge(
                str(result.request_id),
                device_id=slot_result_host,
            ):
                logger.info(
                    "Workstation request %s completed (success=%s)",
                    result.request_id,
                    result.success,
                )
                if slot_result_host == self._get_active_hostname():
                    self._update_sd_keys()
                    self._publish_streamdeck_state()
            return

        if topic.startswith(f"{self.USB_HUB_TOPIC}/port/") and topic.endswith("/set"):
            self._handle_usb_hub_command(topic, payload)

        elif topic == "desk/kvm/set":
            target_pc = payload.strip().upper()
            if target_pc in ["PC1", "1"]:
                self._start_kvm_switch(0)
            elif target_pc in ["PC2", "2"]:
                self._start_kvm_switch(1)
            elif target_pc == "TOGGLE":
                self._start_kvm_switch(1 - self.current_pc)

        elif topic.endswith("/telemetry"):
            if not ingest_mqtt_telemetry(topic, payload):
                logger.warning("Rejected invalid MQTT telemetry on '%s'", topic)

        elif (audio_host := self._audio_state_host(topic)) is not None:
            self._audio_state_by_host[audio_host] = payload.strip()
            acknowledged = self._acknowledge_audio_request(
                audio_host,
                payload.strip(),
            )
            if not self.kvm_fault and audio_host == self._get_active_hostname():
                self._apply_active_audio_state()
                self._update_sd_keys()
                self._publish_streamdeck_state()
            elif acknowledged:
                logger.info(
                    "Confirmed pending audio request for '%s'",
                    audio_host,
                )

        else:
            self._apply_configured_mqtt_state(topic, payload)

    def _set_kvm_fault(self, message: str):
        self.kvm_fault = True
        logger.error(message)
        self.mqtt.publish("desk/kvm/availability", "offline", retain=True)
        self._update_sd_keys()

    @staticmethod
    def _monitor_input_value(input_code: Any) -> Optional[int]:
        if isinstance(input_code, int):
            return input_code
        try:
            return int(str(input_code), 16)
        except (TypeError, ValueError):
            return None

    def _initialize_kvm_state(self) -> bool:
        """Route USB to the host selected on an awake, readable monitor."""
        configured_pc = self.current_pc
        target_pc = configured_pc
        transaction_id = uuid4()
        monitor_owner = self._resolve_kvm_controller(
            "monitor",
            configured_pc,
        )
        usb_owner = self._resolve_kvm_controller("usb", configured_pc)
        if not monitor_owner or not usb_owner:
            self.kvm_fault = True
            logger.error("KVM hardware ownership is not configured correctly")
            return False
        input_source = self._kvm_monitor_get(
            monitor_owner,
            transaction_id,
        )

        if input_source is None:
            logger.info(
                "Monitor input unavailable; using configured fallback host PC%s",
                configured_pc + 1,
            )
        else:
            monitors_cfg = self.config.get("monitors", [{}])
            inputs_cfg = (monitors_cfg[0] if monitors_cfg else {}).get("inputs", {})
            matching_pc = next(
                (
                    pc_index
                    for pc_index in (0, 1)
                    if self._monitor_input_value(
                        inputs_cfg.get(
                            f"pc{pc_index + 1}",
                            "0x0f" if pc_index == 0 else "0x11",
                        )
                    )
                    == input_source
                ),
                None,
            )
            if matching_pc is None:
                logger.warning(
                    "Monitor input 0x%02x does not map to a configured host; "
                    "using fallback PC%s",
                    input_source,
                    configured_pc + 1,
                )
            else:
                target_pc = matching_pc
                logger.info(
                    "Monitor input 0x%02x maps to PC%s; using it as the startup host",
                    input_source,
                    target_pc + 1,
                )

        if not self._kvm_usb_set(usb_owner, target_pc, transaction_id):
            self.kvm_fault = True
            logger.error(
                "%s USB controller refused startup routing to PC%s",
                usb_owner,
                target_pc + 1,
            )
            return False

        self.current_pc = target_pc
        self.kvm_fault = False
        self._refresh_workstation_deck()
        return True

    def _switch_kvm(self, target_pc_index: int) -> bool:
        if target_pc_index not in (0, 1):
            raise ValueError("KVM target must be 0 or 1")

        with self._kvm_lock:
            return self._switch_kvm_transaction(target_pc_index)

    def _switch_kvm_transaction(self, target_pc_index: int) -> bool:
        previous_pc = self.current_pc
        transaction_id = uuid4()
        usb_owner = self._resolve_kvm_controller("usb", previous_pc)
        monitor_owner = self._resolve_kvm_controller(
            "monitor",
            previous_pc,
        )
        logger.info(
            "Switching KVM from PC %s to PC %s", previous_pc + 1, target_pc_index + 1
        )

        if not usb_owner or not monitor_owner:
            self._set_kvm_fault("KVM hardware ownership is not configured correctly")
            return False

        if not self._kvm_usb_set(
            usb_owner,
            target_pc_index,
            transaction_id,
        ):
            self._set_kvm_fault(
                f"USB hub refused KVM switch to PC {target_pc_index + 1}; state was not committed"
            )
            return False

        if not self._kvm_monitor_set(
            monitor_owner,
            target_pc_index,
            transaction_id,
        ):
            rollback_ok = self._kvm_usb_set(
                usb_owner,
                previous_pc,
                transaction_id,
            )
            if rollback_ok:
                message = (
                    f"Monitor refused KVM switch to PC {target_pc_index + 1}; "
                    f"USB hub rolled back to PC {previous_pc + 1}"
                )
            else:
                message = (
                    f"Monitor refused KVM switch to PC {target_pc_index + 1}, "
                    f"and USB rollback to PC {previous_pc + 1} also failed"
                )
            self._set_kvm_fault(message)
            return False

        self.current_pc = target_pc_index
        self.kvm_fault = False

        # Retained state from every workstation is cached as it arrives, so a
        # committed host switch can immediately restore the correct highlight.
        self._refresh_workstation_deck()
        self._apply_active_audio_state()

        kvm_label = f"PC{self.current_pc + 1}"
        if not self.mqtt.publish("desk/kvm/state", kvm_label, retain=True):
            logger.warning("KVM switched successfully, but MQTT state publish failed")
        self.mqtt.publish("desk/kvm/availability", "online", retain=True)

        self._update_sd_keys()
        self._publish_streamdeck_layout()
        self._publish_streamdeck_state()
        return True

    def register_ha_kvm_discovery(self):
        """Registers the KVM switch as an MQTT entity in Home Assistant."""
        if not getattr(self, "homeassistant_enabled", True):
            return
        kvm_select_config = {
            "name": "Desk KVM Active Host",
            "unique_id": "desk_kvm_active_host_select",
            "command_topic": "desk/kvm/set",
            "state_topic": "desk/kvm/state",
            "availability_topic": "desk/kvm/availability",
            "payload_available": "online",
            "payload_not_available": "offline",
            "options": ["PC1", "PC2"],
            "icon": "mdi:monitor-switch",
            "device": self._controller_device_info(),
        }
        if not self.mqtt.publish(
            "homeassistant/select/desk_kvm/config",
            kvm_select_config,
            retain=True,
        ):
            logger.warning("Failed publishing Home Assistant KVM discovery")

    def _configuration_url(self) -> str:
        port = self.config.get("server", {}).get("port", 8080)
        host = self._lan_ip or platform.node()
        return f"http://{host}:{port}/config"

    def _controller_device_info(self) -> Dict[str, Any]:
        return {
            "identifiers": [self.DEVICE_ID],
            "name": "Raspberry Pi Desk Controller",
            "model": "RPi 4 KVM Controller",
            "manufacturer": "Antigravity Smart Desk",
            "sw_version": __version__,
            "configuration_url": self._configuration_url(),
        }

    def _usb_hub_device_info(self) -> Dict[str, Any]:
        return {
            "identifiers": [f"{self.DEVICE_ID}_usb_hub"],
            "name": getattr(
                self.acroname,
                "DISPLAY_NAME",
                "USB Host Switch",
            ),
            "model": getattr(self.acroname, "MODEL", "USB switch"),
            "manufacturer": getattr(
                self.acroname,
                "MANUFACTURER",
                "Unknown",
            ),
            "via_device": self.DEVICE_ID,
            "configuration_url": self._configuration_url(),
        }

    def register_ha_streamdeck_discovery(self):
        """Expose every physical key as a native Home Assistant device trigger."""
        if not getattr(self, "homeassistant_enabled", True):
            return
        action_topic = f"{self.STREAMDECK_TOPIC}/action"
        for key in range(STREAMDECK_KEY_COUNT):
            payload = {
                "automation_type": "trigger",
                "type": "button_short_press",
                "subtype": f"button_{key + 1}",
                "payload": f"key_{key}",
                "topic": action_topic,
                "device": self._controller_device_info(),
            }
            self.mqtt.publish(
                (
                    "homeassistant/device_automation/"
                    f"{self.DEVICE_ID}/streamdeck_key_{key}/config"
                ),
                payload,
                retain=True,
            )

    def register_ha_usb_hub_discovery(self):
        """Expose live hub controls and diagnostics to Home Assistant."""
        if not getattr(self, "homeassistant_enabled", True):
            return
        if not getattr(self.acroname, "SUPPORTS_PORT_CONTROL", True):
            return
        device = self._usb_hub_device_info()

        def publish(component: str, object_id: str, payload: Dict[str, Any]):
            self.mqtt.publish(
                f"homeassistant/{component}/{object_id}/config",
                payload,
                retain=True,
            )

        hub_state_topic = f"{self.USB_HUB_TOPIC}/state"
        hub_entities = [
            (
                "sensor",
                "input_voltage",
                {
                    "name": "Input voltage",
                    "unit_of_measurement": "V",
                    "device_class": "voltage",
                    "state_class": "measurement",
                    "value_template": "{{ value_json.input_voltage_v }}",
                },
            ),
            (
                "sensor",
                "temperature",
                {
                    "name": "Temperature",
                    "unit_of_measurement": "°C",
                    "device_class": "temperature",
                    "state_class": "measurement",
                    "value_template": "{{ value_json.temperature_c }}",
                },
            ),
            (
                "sensor",
                "firmware",
                {
                    "name": "Firmware",
                    "value_template": "{{ value_json.firmware_version }}",
                    "entity_category": "diagnostic",
                },
            ),
        ]
        for component, suffix, entity in hub_entities:
            object_id = f"{self.DEVICE_ID}_usb_hub_{suffix}"
            publish(
                component,
                object_id,
                {
                    **entity,
                    "unique_id": object_id,
                    "state_topic": hub_state_topic,
                    "device": device,
                },
            )

        for port, name in self.usb_ports.items():
            object_prefix = f"{self.DEVICE_ID}_usb_port_{port}"
            state_topic = f"{self.USB_HUB_TOPIC}/port/{port}/state"
            command_topic = f"{self.USB_HUB_TOPIC}/port/{port}/set"

            switches = [
                ("", name, "enabled", "mdi:usb-port"),
                ("_power", f"{name} power", "power_enabled", "mdi:power-plug"),
                ("_usb2", f"{name} USB 2", "usb2_data_enabled", "mdi:usb"),
                ("_usb3", f"{name} USB 3", "usb3_data_enabled", "mdi:usb-c-port"),
            ]
            for suffix, entity_name, field, icon in switches:
                object_id = f"{object_prefix}{suffix}"
                publish(
                    "switch",
                    object_id,
                    {
                        "name": entity_name,
                        "unique_id": object_id,
                        "icon": icon,
                        "command_topic": command_topic,
                        "state_topic": state_topic,
                        "value_template": (
                            f"{{{{ 'ON' if value_json.{field} else 'OFF' }}}}"
                        ),
                        "payload_on": json.dumps({field: True}),
                        "payload_off": json.dumps({field: False}),
                        "device": device,
                    },
                )

            sensors = [
                (
                    "device",
                    f"{name} device",
                    (
                        "{{ value_json.device.product_name "
                        "if value_json.device else 'None' }}"
                    ),
                    None,
                    None,
                ),
                (
                    "current",
                    f"{name} current",
                    "{{ value_json.current_a }}",
                    "A",
                    "current",
                ),
                (
                    "voltage",
                    f"{name} voltage",
                    "{{ value_json.voltage_v }}",
                    "V",
                    "voltage",
                ),
                (
                    "power",
                    f"{name} power draw",
                    "{{ value_json.power_w }}",
                    "W",
                    "power",
                ),
                (
                    "speed",
                    f"{name} data speed",
                    "{{ value_json.data_speed }}",
                    None,
                    None,
                ),
                (
                    "errors",
                    f"{name} errors",
                    "{{ value_json.errors | join(', ') or 'None' }}",
                    None,
                    None,
                ),
            ]
            for suffix, entity_name, template, unit, device_class in sensors:
                object_id = f"{object_prefix}_{suffix}"
                payload = {
                    "name": entity_name,
                    "unique_id": object_id,
                    "state_topic": state_topic,
                    "value_template": template,
                    "device": device,
                }
                if unit:
                    payload["unit_of_measurement"] = unit
                    payload["state_class"] = "measurement"
                if device_class:
                    payload["device_class"] = device_class
                publish("sensor", object_id, payload)

            attached_id = f"{object_prefix}_attached"
            publish(
                "binary_sensor",
                attached_id,
                {
                    "name": f"{name} attached",
                    "unique_id": attached_id,
                    "state_topic": state_topic,
                    "value_template": (
                        "{{ 'ON' if value_json.device_attached else 'OFF' }}"
                    ),
                    "device_class": "connectivity",
                    "device": device,
                },
            )

            limit_id = f"{object_prefix}_current_limit"
            publish(
                "number",
                limit_id,
                {
                    "name": f"{name} current limit",
                    "unique_id": limit_id,
                    "state_topic": state_topic,
                    "value_template": (
                        "{{ (value_json.current_limit_a * 1000) | int }}"
                    ),
                    "command_topic": command_topic,
                    "command_template": ('{"current_limit_ma": {{ value | int }}}'),
                    "min": 0,
                    "max": self.acroname.MAX_CURRENT_LIMIT_MA,
                    "step": 1,
                    "unit_of_measurement": "mA",
                    "device": device,
                },
            )

            mode_id = f"{object_prefix}_power_mode"
            publish(
                "select",
                mode_id,
                {
                    "name": f"{name} charging mode",
                    "unique_id": mode_id,
                    "state_topic": state_topic,
                    "value_template": (
                        "{{ 'CDP' if value_json.mode == 1 else 'SDP' }}"
                    ),
                    "command_topic": command_topic,
                    "command_template": (
                        "{\"port_mode\": {{ 1 if value == 'CDP' else 0 }}}"
                    ),
                    "options": ["SDP", "CDP"],
                    "device": device,
                },
            )

            for action, label, icon in (
                ("reset_data", "Re-enumerate", "mdi:restart"),
                ("reset_power", "Power cycle", "mdi:power-cycle"),
                ("clear_errors", "Clear errors", "mdi:alert-circle-check"),
            ):
                object_id = f"{object_prefix}_{action}"
                publish(
                    "button",
                    object_id,
                    {
                        "name": f"{name} {label}",
                        "unique_id": object_id,
                        "icon": icon,
                        "command_topic": command_topic,
                        "payload_press": json.dumps({"action": action}),
                        "device": device,
                    },
                )

    def _publish_streamdeck_layout(self):
        buttons = []
        physical_buttons = getattr(self, "physical_buttons", {})
        for key in range(STREAMDECK_KEY_COUNT):
            configured = key in physical_buttons
            button = self.buttons.get(key, {})
            buttons.append(
                {
                    "key": key,
                    "row": key // 5,
                    "column": key % 5,
                    "configured": configured,
                    **button,
                }
            )
        self.mqtt.publish(
            f"{self.STREAMDECK_TOPIC}/layout",
            {
                "rows": 3,
                "columns": 5,
                "buttons": buttons,
            },
            retain=True,
        )

    def _publish_streamdeck_state(self):
        active_workstation = self._get_active_hostname()
        workstation_state = (
            self.workstation_deck.selected_state(active_workstation)
            if hasattr(self, "workstation_deck")
            else {
                "device_id": active_workstation,
                "agent_online": False,
                "active": {},
            }
        )
        self.mqtt.publish(
            f"{self.STREAMDECK_TOPIC}/state",
            {
                "active_groups": self.active_groups,
                "active_audio_device": self.active_audio_device or None,
                "active_host": f"PC{self.current_pc + 1}",
                "active_workstation": workstation_state,
                "kvm_fault": self.kvm_fault,
            },
            retain=True,
        )

    def _publish_button_event(self, key: int, button: Dict[str, Any]):
        action_payload = f"key_{key}"
        self.mqtt.publish(
            f"{self.STREAMDECK_TOPIC}/action",
            action_payload,
            qos=1,
        )
        self.mqtt.publish(
            f"{self.STREAMDECK_TOPIC}/event",
            {
                "event": "press",
                "key": key,
                "button_number": key + 1,
                "row": key // 5,
                "column": key % 5,
                "configured": bool(button),
                "label": button.get("label", f"Key {key + 1}"),
                "group": button.get("group", ""),
                "action_type": button.get("action_type", "none"),
                "slot_id": button.get("slot_id"),
                "workstation_id": button.get("_workstation_id"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
            qos=1,
        )

    def _publish_usb_hub_config(self):
        supported_commands = getattr(
            self.acroname,
            "SUPPORTED_COMMANDS",
            None,
        )
        if not isinstance(supported_commands, (list, tuple)):
            supported_commands = (
                "enabled",
                "power_enabled",
                "data_enabled",
                "usb2_data_enabled",
                "usb3_data_enabled",
                "current_limit_ma",
                "port_mode",
                "reset_data",
                "reset_power",
                "clear_errors",
            )
        self.mqtt.publish(
            f"{self.USB_HUB_TOPIC}/config",
            {
                "driver": getattr(self.acroname, "DRIVER", "acroname"),
                "port_count": self.acroname.PORT_COUNT,
                "telemetry_interval": self._hub_telemetry_interval,
                "sentinel_observation": self._usb_sentinel_config().model_dump(
                    mode="json"
                ),
                "supported_commands": list(supported_commands),
                "ports": [
                    {"index": port, "name": name}
                    for port, name in self.usb_ports.items()
                    if port < self.acroname.PORT_COUNT
                ],
            },
            retain=True,
        )

    def _publish_usb_hub_state(self):
        status = self._get_usb_hub_status()
        self.mqtt.publish(
            f"{self.USB_HUB_TOPIC}/state",
            status,
            retain=True,
        )
        for port in status["ports"]:
            self.mqtt.publish(
                f"{self.USB_HUB_TOPIC}/port/{port['index']}/state",
                port,
                retain=True,
            )
            if "enabled" in port:
                self.mqtt.publish(
                    f"{self.USB_HUB_TOPIC}/port/{port['index']}/enabled",
                    "ON" if port["enabled"] else "OFF",
                    retain=True,
                )
        self._last_hub_telemetry = time.monotonic()
        return status

    def _get_usb_hub_status(self) -> Dict[str, Any]:
        status = self.acroname.get_hub_status(self.usb_ports)
        driver = getattr(self.acroname, "DRIVER", "acroname")
        if driver == "ugreen_cm691_gpio":
            sentinel = self._usb_sentinel_config()
            observed = self._observed_usb_channel() if sentinel.is_complete() else None
            status["desired_upstream"] = self.current_pc
            status["observed_upstream"] = observed
            status["observation_enabled"] = sentinel.is_complete()
            if not sentinel.is_complete():
                status["sync_status"] = "unverified"
            elif observed is None:
                status["sync_status"] = "unknown"
            elif observed == self.current_pc:
                status["sync_status"] = "synced"
            else:
                status["sync_status"] = "diverged"
            status["active_upstream"] = observed
            status["state_feedback"] = sentinel.is_complete()
            status.setdefault("capabilities", {})["state_feedback"] = (
                sentinel.is_complete()
            )
        else:
            observed = status.get("active_upstream")
            status["desired_upstream"] = self.current_pc
            status["observed_upstream"] = observed
            status["sync_status"] = (
                "synced" if observed == self.current_pc else "diverged"
            )
        return status

    def _control_usb_hub(
        self,
        settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        if "name" in settings:
            success = self.acroname.set_name(settings["name"])
            return {
                "success": success,
                "error": None if success else "Hub name update failed",
                "state": self._get_usb_hub_status(),
            }
        target = settings.get("active_upstream")
        if target not in (0, 1):
            return {"success": False, "error": "Invalid upstream host"}
        success = self._switch_kvm(target)
        return {
            "success": success,
            "error": None if success else "KVM switch failed",
            "state": self._get_usb_hub_status(),
        }

    def _control_usb_hub_port(
        self,
        port: int,
        settings: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not getattr(self.acroname, "SUPPORTS_PORT_CONTROL", True):
            return {
                "port": port,
                "success": False,
                "error": (
                    f"{getattr(self.acroname, 'DISPLAY_NAME', 'USB switch')} "
                    "does not support per-port control"
                ),
            }
        if not isinstance(port, int) or not 0 <= port < self.acroname.PORT_COUNT:
            return {
                "port": port,
                "success": False,
                "error": (
                    f"USB port must be between 0 and {self.acroname.PORT_COUNT - 1}"
                ),
            }

        action = settings.get("action")
        if action is not None:
            if set(settings) != {"action"}:
                success = False
                error = "USB hub actions cannot be combined with settings"
            elif action == "reset_data":
                success = self.acroname.reset_port(port, reset_type="data")
                error = "" if success else "USB data reset failed"
            elif action == "reset_power":
                success = self.acroname.reset_port(port, reset_type="power")
                error = "" if success else "USB power reset failed"
            elif action == "clear_errors":
                success = self.acroname.clear_port_errors(port)
                error = "" if success else "Clearing USB port errors failed"
            else:
                success = False
                error = "Unknown USB hub action"
        else:
            allowed = {
                "enabled",
                "power_enabled",
                "data_enabled",
                "usb2_data_enabled",
                "usb3_data_enabled",
                "current_limit_ma",
                "port_mode",
            }
            if not settings or set(settings) - allowed:
                success = False
                error = "Invalid USB hub command"
            else:
                bool_fields_valid = all(
                    field not in settings or isinstance(settings[field], bool)
                    for field in (
                        "enabled",
                        "power_enabled",
                        "data_enabled",
                        "usb2_data_enabled",
                        "usb3_data_enabled",
                    )
                )
                limit = settings.get("current_limit_ma")
                limit_valid = limit is None or (
                    isinstance(limit, int)
                    and not isinstance(limit, bool)
                    and 0 <= limit <= self.acroname.MAX_CURRENT_LIMIT_MA
                )
                port_mode = settings.get("port_mode")
                mode_valid = port_mode is None or (
                    isinstance(port_mode, int)
                    and not isinstance(port_mode, bool)
                    and port_mode in (0, 1)
                )
                if not bool_fields_valid or not limit_valid or not mode_valid:
                    success = False
                    error = "Invalid USB hub command values"
                else:
                    success = self.acroname.set_port_settings(
                        port,
                        **settings,
                    )
                    error = "" if success else "USB hub rejected the command"

        result = {
            "port": port,
            "name": self.usb_ports.get(port, f"USB Port {port}"),
            "success": success,
            "error": error or None,
        }
        self.mqtt.publish(
            f"{self.USB_HUB_TOPIC}/command_result",
            result,
        )
        status = self._publish_usb_hub_state()
        result["state"] = next(
            (
                port_state
                for port_state in status.get("ports", [])
                if port_state.get("index") == port
            ),
            None,
        )
        return result

    def _handle_usb_hub_command(self, topic: str, payload: str):
        try:
            port = int(topic.split("/")[-2])
        except (ValueError, IndexError):
            return

        raw_payload = payload.strip()
        if raw_payload.upper() in {"ON", "OFF"}:
            settings = {"enabled": raw_payload.upper() == "ON"}
        else:
            try:
                settings = json.loads(raw_payload)
            except json.JSONDecodeError:
                settings = {}

        if not isinstance(settings, dict):
            settings = {}
        return self._control_usb_hub_port(port, settings)

    @staticmethod
    def _audio_names_match(active: str, configured: str) -> bool:
        active = active.strip().lower()
        configured = configured.strip().lower()
        return bool(
            active and configured and (active in configured or configured in active)
        )

    def _apply_configured_mqtt_state(self, topic: str, payload: str):
        matching_key = None
        matching_group = ""
        groups_on_topic = set()
        for key, button in self.buttons.items():
            state_topic = str(button.get("state_topic", "")).strip()
            group = str(button.get("group", "")).strip()
            if state_topic != topic or not group:
                continue
            groups_on_topic.add(group)
            if payload.strip() == str(button.get("state_payload", "")):
                matching_key = key
                matching_group = group

        changed = False
        if matching_key is not None:
            changed = self.active_groups.get(matching_group) != matching_key
            self.active_groups[matching_group] = matching_key
        else:
            for group in groups_on_topic:
                if group in self.active_groups:
                    del self.active_groups[group]
                    changed = True
        if changed:
            self._update_sd_keys()
            self._publish_streamdeck_state()

    def _handle_key_press(self, key: int):
        logger.info("Stream Deck key pressed: %s", key)
        with self._kvm_lock:
            button = dict(self.buttons.get(key, {}))
            selected_workstation = self._get_active_hostname()
        self._publish_button_event(key, button)
        if not button or not button.get("enabled", True):
            self._publish_streamdeck_state()
            return

        action_type = button.get("action_type", "none")
        target = str(button.get("target", "")).strip()
        group = str(button.get("group", "")).strip()
        action_succeeded = False

        if action_type == "kvm_toggle":
            next_pc = 1 if self.current_pc == 0 else 0
            action_succeeded = self._switch_kvm(next_pc)

        elif action_type == "audio_output":
            if self.kvm_fault:
                logger.error("Ignoring audio switch while KVM state is uncertain")
            elif target:
                action_succeeded = self._queue_workstation_request(
                    key,
                    selected_workstation,
                    action_type,
                    target,
                )
            if not action_succeeded:
                logger.warning("Failed queuing audio switch command")

        elif action_type == "ha_scene":
            off_target = str(button.get("off_target", "")).strip()
            if not getattr(self, "homeassistant_enabled", True):
                logger.warning(
                    "Ignoring Home Assistant action on key %s because the "
                    "integration is disabled",
                    key,
                )
            elif group and self.active_groups.get(group) == key and off_target:
                action_succeeded = self.ha.trigger_automation(off_target)
                if action_succeeded:
                    self.active_groups.pop(group, None)
            elif target:
                action_succeeded = self.ha.activate_scene(target)
                if action_succeeded and group:
                    self.active_groups[group] = key

        elif action_type == "ha_service":
            if not getattr(self, "homeassistant_enabled", True):
                logger.warning(
                    "Ignoring Home Assistant action on key %s because the "
                    "integration is disabled",
                    key,
                )
            else:
                action_succeeded = self.ha.call_service(
                    str(button.get("service", "")),
                    target,
                    button.get("service_data", {}),
                )
                if action_succeeded and group:
                    self.active_groups[group] = key

        elif action_type == "mqtt":
            mqtt_topic = str(button.get("mqtt_topic", "")).strip()
            if mqtt_topic:
                action_succeeded = self.mqtt.publish(
                    mqtt_topic,
                    button.get("mqtt_payload", ""),
                )
                if action_succeeded and group:
                    self.active_groups[group] = key

        elif action_type == "workstation_slot":
            slot_id = str(button.get("slot_id", "")).strip()
            if button.get("_slot_configured") and slot_id:
                action_succeeded = self._queue_workstation_request(
                    key,
                    selected_workstation,
                    action_type,
                    slot_id,
                )
            else:
                logger.warning(
                    "Ignoring unavailable workstation slot '%s' for '%s'",
                    slot_id,
                    selected_workstation,
                )

        elif action_type == "none" or action_type in DYNAMIC_DATETIME_ACTIONS:
            action_succeeded = True

        if (
            action_succeeded
            and group
            and action_type
            not in {
                "audio_output",
                "ha_scene",
                "ha_service",
                "mqtt",
                "workstation_slot",
                *DYNAMIC_DATETIME_ACTIONS,
            }
        ):
            self.active_groups[group] = key

        self._update_sd_keys()
        self._publish_streamdeck_state()

    def _update_sd_keys(self):
        selected_workstation = self._get_active_hostname()
        selected_agent_online = self._selected_agent_online()
        rendered_at = datetime.now().astimezone()
        for key in range(STREAMDECK_KEY_COUNT):
            button = self.buttons.get(key)
            if not button or not button.get("enabled", True):
                self.streamdeck.render_scene_key(
                    key=key,
                    label="",
                    icon_type="none",
                    is_active=False,
                )
                continue

            action_type = button.get("action_type", "none")
            group = str(button.get("group", "")).strip()
            label = str(button.get("label", ""))
            if action_type in DYNAMIC_DATETIME_ACTIONS:
                label = datetime_button_label(action_type, rendered_at)
            color = tuple(button.get("accent_color", [100, 100, 110]))
            active = bool(group and self.active_groups.get(group) == key)
            is_available = None
            is_pending = self._key_has_pending_request(
                selected_workstation,
                key,
            )

            if action_type == "audio_output":
                is_available = bool(selected_agent_online and not self.kvm_fault)
                active = bool(
                    is_available
                    and self._audio_names_match(
                        self.active_audio_device,
                        str(button.get("target", "")),
                    )
                )
            elif action_type == "kvm_toggle":
                label = (
                    "KVM\nERROR"
                    if self.kvm_fault
                    else f"HOST\nPC {self.current_pc + 1}"
                )
                active = self.kvm_fault
                if self.kvm_fault:
                    color = (255, 60, 60)
            elif action_type == "workstation_slot":
                is_available = bool(
                    button.get("_agent_online") and button.get("_slot_configured")
                )
                active = bool(button.get("_slot_active") and is_available)

            self.streamdeck.render_scene_key(
                key=key,
                label=label,
                icon_type=str(button.get("icon", "none")),
                is_active=active,
                accent_color=color,
                host_num=self.current_pc + 1,
                is_available=is_available,
                is_pending=is_pending,
                display_style=(
                    action_type if action_type in DYNAMIC_DATETIME_ACTIONS else "button"
                ),
            )

    def _refresh_datetime_buttons(self, now: Optional[datetime] = None) -> bool:
        """Redraw date/time buttons when the displayed local minute changes."""
        if not any(
            button.get("enabled", True)
            and button.get("action_type") in DYNAMIC_DATETIME_ACTIONS
            for button in self.buttons.values()
        ):
            return False

        local_now = now or datetime.now().astimezone()
        minute = local_now.strftime("%Y-%m-%d %H:%M")
        if minute == self._clock_display_minute:
            return False

        self._clock_display_minute = minute
        self._update_sd_keys()
        return True

    def _start_api_server(self):
        """Start telemetry and configuration HTTP routes in the background."""
        server_conf = self.config.get("server", {})
        host = server_conf.get("host", "127.0.0.1")
        port = server_conf.get("port", 8080)
        if str(host).strip().lower() not in {
            "127.0.0.1",
            "::1",
            "localhost",
        }:
            logger.warning(
                "The unauthenticated HTTP API is listening on %s. "
                "Use loopback unless an authenticated reverse proxy and "
                "firewall protect this endpoint.",
                host,
            )
        api_thread = threading.Thread(
            target=uvicorn.run,
            kwargs={
                "app": app,
                "host": host,
                "port": port,
                "log_level": "warning",
            },
            daemon=True,
        )
        api_thread.start()

    def run(self):
        logger.info("Starting MQTT Desk Controller Application...")
        self._start_api_server()

        # Only connect hardware physically assigned to the Pi. Remote hardware
        # steps are available after MQTT starts below.
        local_usb_ready = True
        if self._pi_controls_usb():
            local_usb_ready = self.acroname.connect()
            if not local_usb_ready:
                self.kvm_fault = True
        self.streamdeck.initialize()
        self._update_sd_keys()
        self._clock_display_minute = (
            datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
        )

        # Remember subscriptions before connecting so on_connect can apply them.
        self.mqtt.subscribe("desk/+/audio/state")
        self.mqtt.subscribe("desk/+/telemetry")
        for workstation_id in self._configured_workstation_ids():
            self.mqtt.subscribe(
                f"desk/{workstation_id}/availability",
                qos=1,
            )
            self.mqtt.subscribe(
                f"desk/{workstation_id}/deck/manifest",
                qos=1,
            )
            self.mqtt.subscribe(
                f"desk/{workstation_id}/deck/state",
                qos=1,
            )
            self.mqtt.subscribe(
                slot_result_topic(workstation_id),
                qos=1,
            )
            self.mqtt.subscribe(
                kvm_hardware_result_topic(workstation_id),
                qos=1,
            )
            self.mqtt.subscribe(
                usb_observation_topic(workstation_id),
                qos=1,
            )
        self.mqtt.subscribe("desk/kvm/set")
        self.mqtt.subscribe(KVM_REQUEST_TOPIC, qos=1)
        if getattr(self, "homeassistant_enabled", True):
            self.mqtt.subscribe("homeassistant/status")
        self.mqtt.subscribe(f"{self.USB_HUB_TOPIC}/port/+/set")
        for button in self.buttons.values():
            state_topic = str(button.get("state_topic", "")).strip()
            if state_topic:
                self.mqtt.subscribe(state_topic)
        mqtt_started = self.mqtt.start()
        if not mqtt_started:
            logger.error("MQTT network loop failed to start")
        elif not self.mqtt.wait_until_connected(timeout=10):
            logger.warning(
                "MQTT did not connect within 10 seconds; retrying in background"
            )
        if local_usb_ready:
            self._reconcile_kvm_state()

        try:
            while not self._stop_event.is_set():
                self._refresh_datetime_buttons()
                if self._process_pending_workstation_requests():
                    self._update_sd_keys()
                    self._publish_streamdeck_state()
                if (
                    time.monotonic() - self._last_hub_telemetry
                    >= self._hub_telemetry_interval
                ):
                    self._publish_usb_hub_state()
                self._stop_event.wait(2)
        except KeyboardInterrupt:
            logger.info("Stopping application...")
        finally:
            self.mqtt.stop()
            self.streamdeck.close()
            self.acroname.disconnect()


def main():
    app_instance = DeskControllerApp()
    app_instance.run()


if __name__ == "__main__":
    main()
