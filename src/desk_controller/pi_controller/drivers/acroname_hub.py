"""
Acroname USB Hub Driver using BrainStem SDK.
"""

import logging
import threading
import time
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

try:
    import brainstem
    from brainstem.result import Result

    BRAINSTEM_AVAILABLE = True
except ImportError:
    BRAINSTEM_AVAILABLE = False
    logger.warning(
        "Acroname 'brainstem' module is not installed; hardware simulation must be enabled explicitly."
    )


class AcronameHubController:
    """Manages host channel switching and port control on Acroname USB Hubs (USBHub3p & USBHub2x4)."""

    PORT_COUNT = 8
    MAX_CURRENT_LIMIT_MA = 4095
    DRIVER = "acroname"
    MANUFACTURER = "Acroname"
    MODEL = "USBHub3p"
    DISPLAY_NAME = "Acroname USB Hub"
    SUPPORTS_PORT_CONTROL = True
    SUPPORTED_COMMANDS = (
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
    ERROR_FLAGS = {
        0: "overcurrent",
        1: "backdrive",
        2: "external_power_missing",
        3: "overtemperature",
        4: "short_circuit",
    }

    def __init__(self, serial_number: int = None, simulate: bool = False):
        self.serial_number = serial_number
        self.simulate = simulate
        self.stem = None
        self.connected = False
        self.active_channel = 0
        self._simulated_name = "Simulated USBHub3p"
        self._lock = threading.RLock()
        self._simulated_ports = {
            port: {
                "enabled": True,
                "power_enabled": True,
                "usb2_data_enabled": True,
                "usb3_data_enabled": True,
                "device_attached": False,
                "error": False,
                "errors": [],
                "voltage_v": 5.0,
                "current_a": 0.0,
                "current_limit_a": 2.5,
                "mode": 0,
                "data_speed": 0,
                "raw_state": 0,
                "devices": [],
                "device": None,
            }
            for port in range(self.PORT_COUNT)
        }

    def connect(self) -> bool:
        with self._lock:
            if self.simulate:
                logger.info("[Simulation] Connected to Acroname USB Hub")
                self.connected = True
                return True

            if not BRAINSTEM_AVAILABLE:
                logger.error(
                    "Cannot connect to Acroname USB Hub: 'brainstem' is not installed"
                )
                return False

            try:
                # Try USBHub3p first, fall back to USBHub2x4
                if hasattr(brainstem.stem, "USBHub3p"):
                    self.stem = brainstem.stem.USBHub3p()
                else:
                    self.stem = brainstem.stem.USBHub2x4()

                if hasattr(self.stem, "discoverAndConnect"):
                    res = self.stem.discoverAndConnect(
                        brainstem.link.Spec.USB,
                        self.serial_number or 0,
                    )
                else:
                    res = self.stem.connect(
                        brainstem.link.Spec.USB,
                        self.serial_number or 0,
                    )

                if res == Result.NO_ERROR:
                    self.connected = True
                    logger.info("Successfully connected to Acroname USB Hub")
                    return True
                else:
                    logger.error(f"Failed to connect to Acroname Hub, code: {res}")
                    return False
            except Exception as e:
                logger.error(f"Error initializing Acroname Hub: {e}")
                return False

    def switch_upstream_channel(self, channel: int) -> bool:
        """Switch USB upstream connection (0 = PC1, 1 = PC2)."""
        if channel not in (0, 1):
            logger.error("Invalid Acroname upstream channel: %s", channel)
            return False
        with self._lock:
            if self.simulate:
                self.active_channel = channel
                logger.info(
                    "[Simulation] Acroname switched to Upstream Channel %s", channel
                )
                return True
            if not BRAINSTEM_AVAILABLE or not self.connected:
                logger.error("Cannot switch Acroname channel: hub is not connected")
                return False

            try:
                if hasattr(self.stem.usb, "setUpstreamMode"):
                    res = self.stem.usb.setUpstreamMode(channel)
                else:
                    res = self.stem.usb.setUpstreamState(channel)

                if res == Result.NO_ERROR:
                    self.active_channel = channel
                    logger.info(
                        f"Switched Acroname upstream host channel to: {channel}"
                    )
                    return True
                else:
                    logger.error(f"Failed setting upstream state/mode, error: {res}")
                    return False
            except Exception as e:
                logger.error(f"Failed to switch Acroname channel: {e}")
                return False

    @staticmethod
    def _serializable(value: Any) -> Any:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").rstrip("\x00")
        return value

    @staticmethod
    def _valid_port(port: int) -> bool:
        return 0 <= port < AcronameHubController.PORT_COUNT

    def _read_result(self, method_name: str, *args) -> Optional[Any]:
        try:
            method = getattr(self.stem.usb, method_name)
            result = method(*args)
            if result.error != Result.NO_ERROR:
                logger.warning(
                    "Acroname %s%r failed with code %s",
                    method_name,
                    args,
                    result.error,
                )
                return None
            return self._serializable(result.value)
        except Exception:
            logger.exception("Acroname %s%r failed", method_name, args)
            return None

    def _write_result(self, target: Any, method_name: str, *args) -> bool:
        try:
            result = getattr(target, method_name)(*args)
        except Exception:
            logger.exception("Acroname %s%r failed", method_name, args)
            return False
        if result != Result.NO_ERROR:
            logger.error(
                "Acroname %s%r failed with code %s",
                method_name,
                args,
                result,
            )
            return False
        return True

    def _read_system_result(self, method_name: str) -> Optional[Any]:
        try:
            method = getattr(self.stem.system, method_name)
            result = method()
            if result.error != Result.NO_ERROR:
                return None
            return self._serializable(result.value)
        except Exception:
            return None

    def _read_temperature(self) -> Optional[Any]:
        value = self._read_system_result("getTemperature")
        if value is not None:
            return value
        try:
            result = self.stem.temperature[0].getValue()
            if result.error == Result.NO_ERROR:
                return self._serializable(result.value)
        except Exception:
            return None
        return None

    @staticmethod
    def _format_firmware_version(value: Optional[Any]) -> Optional[Any]:
        if value is None or not BRAINSTEM_AVAILABLE:
            return value
        try:
            return brainstem.version.get_version_string(value)
        except Exception:
            return value

    @staticmethod
    def _device_node_value(node: Any, name: str, default: Any = None) -> Any:
        value = getattr(node, name, default)
        return AcronameHubController._serializable(value)

    def _get_downstream_devices(
        self,
        hub_serial_number: Optional[int],
    ) -> Dict[int, list]:
        """Return USB descriptors grouped by physical downstream hub port."""
        devices = {port: [] for port in range(self.PORT_COUNT)}
        if self.simulate or not BRAINSTEM_AVAILABLE or not self.connected:
            return devices

        try:
            result = brainstem.discover.getDownstreamDevices()
            if result.error != Result.NO_ERROR:
                logger.debug(
                    "Acroname downstream device discovery failed with code %s",
                    result.error,
                )
                return devices
            nodes = result.value or ()
        except Exception:
            logger.exception("Acroname downstream device discovery failed")
            return devices

        for node in nodes:
            port = self._device_node_value(node, "hub_port")
            node_hub_serial = self._device_node_value(
                node,
                "hub_serial_number",
            )
            if (
                not isinstance(port, int)
                or port not in devices
                or (
                    hub_serial_number is not None
                    and node_hub_serial != hub_serial_number
                )
            ):
                continue
            vendor_id = self._device_node_value(node, "id_vendor", 0)
            product_id = self._device_node_value(node, "id_product", 0)
            devices[port].append(
                {
                    "vendor_id": vendor_id,
                    "product_id": product_id,
                    "vid_pid": f"{int(vendor_id):04x}:{int(product_id):04x}",
                    "product_name": self._device_node_value(
                        node,
                        "product_name",
                        "",
                    ),
                    "manufacturer": self._device_node_value(
                        node,
                        "manufacture",
                        "",
                    ),
                    "serial_number": self._device_node_value(
                        node,
                        "serial_number",
                        "",
                    ),
                    "speed": self._device_node_value(node, "speed", 0),
                }
            )
        return devices

    def get_port_status(
        self,
        port: int,
        devices: Optional[list] = None,
    ) -> Dict[str, Any]:
        """Read controllable state and electrical telemetry for one port."""
        if not self._valid_port(port):
            raise ValueError("Acroname port must be between 0 and 7")

        with self._lock:
            if self.simulate:
                state = {"index": port, **self._simulated_ports[port]}
                state["power_w"] = round(
                    state["voltage_v"] * state["current_a"],
                    4,
                )
                return state
            if not BRAINSTEM_AVAILABLE or not self.connected:
                return {"index": port, "available": False}

            raw_state = self._read_result("getPortState", port)
            voltage = self._read_result("getPortVoltage", port)
            current = self._read_result("getPortCurrent", port)
            current_limit = self._read_result("getPortCurrentLimit", port)
            mode = self._read_result("getPortMode", port)
            data_speed = self._read_result("getDownstreamDataSpeed", port)
            raw_errors = self._read_result("getPortError", port)

            state = int(raw_state or 0)
            error_state = int(raw_errors or 0)

            def state_flag(name: str) -> bool:
                bit = int(getattr(self.stem, name, 63))
                return bool(state & (1 << bit))

            power_enabled = state_flag("aUSBHUB3P_USB_VBUS_ENABLED")
            usb2_enabled = state_flag("aUSBHUB3P_USB2_DATA_ENABLED")
            usb3_enabled = state_flag("aUSBHUB3P_USB3_DATA_ENABLED")
            voltage_v = (
                round(float(voltage) / 1_000_000, 4) if voltage is not None else None
            )
            current_a = (
                round(float(current) / 1_000_000, 4) if current is not None else None
            )
            attached_devices = devices or []
            return {
                "index": port,
                "available": raw_state is not None,
                "enabled": power_enabled and (usb2_enabled or usb3_enabled),
                "power_enabled": power_enabled,
                "usb2_data_enabled": usb2_enabled,
                "usb3_data_enabled": usb3_enabled,
                "device_attached": state_flag("aUSBHUB3P_DEVICE_ATTACHED"),
                "error": state_flag("aUSBHUB3P_USB_ERROR_FLAG"),
                "errors": [
                    name
                    for bit, name in self.ERROR_FLAGS.items()
                    if error_state & (1 << bit)
                ],
                "raw_error": raw_errors,
                "voltage_v": voltage_v,
                "current_a": current_a,
                "power_w": (
                    round(voltage_v * current_a, 4)
                    if voltage_v is not None and current_a is not None
                    else None
                ),
                "current_limit_a": (
                    round(float(current_limit) / 1_000_000, 4)
                    if current_limit is not None
                    else None
                ),
                "mode": mode,
                "data_speed": data_speed,
                "raw_state": raw_state,
                "devices": attached_devices,
                "device": attached_devices[0] if attached_devices else None,
            }

    def get_hub_status(
        self,
        port_names: Optional[Mapping[int, str]] = None,
    ) -> Dict[str, Any]:
        """Return hub identity plus state for all eight downstream ports."""
        names = port_names or {}
        with self._lock:
            if self.simulate:
                identity = {
                    "model": "USBHub3p (simulated)",
                    "serial_number": self.serial_number,
                    "firmware_version": None,
                    "hardware_version": None,
                    "input_voltage_v": 5.0,
                    "input_current_a": 0.0,
                    "input_power_w": 0.0,
                    "temperature_c": None,
                    "uptime_seconds": None,
                    "name": self._simulated_name,
                }
            elif not BRAINSTEM_AVAILABLE or not self.connected:
                identity = {
                    "model": None,
                    "serial_number": self.serial_number,
                    "firmware_version": None,
                    "hardware_version": None,
                    "input_voltage_v": None,
                    "input_current_a": None,
                    "input_power_w": None,
                    "temperature_c": None,
                    "uptime_seconds": None,
                    "name": None,
                }
            else:
                input_voltage = self._read_system_result("getInputVoltage")
                input_current = self._read_system_result("getInputCurrent")
                temperature = self._read_temperature()
                serial_number = self._read_system_result("getSerialNumber")
                input_voltage_v = (
                    round(float(input_voltage) / 1_000_000, 4)
                    if input_voltage is not None
                    else None
                )
                input_current_a = (
                    round(float(input_current) / 1_000_000, 4)
                    if input_current is not None
                    else None
                )
                upstream_state = self._read_result("getUpstreamState")
                if upstream_state in (0, 1):
                    self.active_channel = int(upstream_state)
                identity = {
                    "model": self._read_system_result("getModel"),
                    "serial_number": serial_number,
                    "firmware_version": self._format_firmware_version(
                        self._read_system_result("getVersion")
                    ),
                    "hardware_version": self._read_system_result("getHardwareVersion"),
                    "input_voltage_v": input_voltage_v,
                    "input_current_a": input_current_a,
                    "input_power_w": (
                        round(input_voltage_v * input_current_a, 4)
                        if input_voltage_v is not None and input_current_a is not None
                        else None
                    ),
                    "temperature_c": (
                        round(float(temperature) / 1_000_000, 2)
                        if temperature is not None
                        else None
                    ),
                    "uptime_seconds": self._read_system_result("getUptime"),
                    "name": self._read_system_result("getName"),
                }

            descriptors = self._get_downstream_devices(
                identity["serial_number"] or self.serial_number,
            )
            ports = []
            for port in range(self.PORT_COUNT):
                status = self.get_port_status(port, descriptors.get(port))
                status["name"] = names.get(port, f"USB Port {port}")
                ports.append(status)

            return {
                "connected": self.connected,
                "driver": self.DRIVER,
                "manufacturer": self.MANUFACTURER,
                "active_upstream": self.active_channel,
                "state_feedback": True,
                "capabilities": {
                    "upstream_switch": True,
                    "per_port_control": True,
                    "telemetry": True,
                    "state_feedback": True,
                },
                **identity,
                "ports": ports,
            }

    def set_port_settings(
        self,
        port: int,
        *,
        enabled: Optional[bool] = None,
        power_enabled: Optional[bool] = None,
        data_enabled: Optional[bool] = None,
        usb2_data_enabled: Optional[bool] = None,
        usb3_data_enabled: Optional[bool] = None,
        current_limit_ma: Optional[int] = None,
        port_mode: Optional[int] = None,
    ) -> bool:
        """Apply supported runtime controls to one downstream port."""
        if not self._valid_port(port):
            return False
        if (
            current_limit_ma is not None
            and not 0 <= current_limit_ma <= self.MAX_CURRENT_LIMIT_MA
        ):
            return False
        if port_mode is not None and port_mode not in (0, 1):
            return False

        requested = [
            enabled is not None,
            power_enabled is not None,
            data_enabled is not None,
            usb2_data_enabled is not None,
            usb3_data_enabled is not None,
            current_limit_ma is not None,
            port_mode is not None,
        ]
        if not any(requested):
            return False

        with self._lock:
            if self.simulate:
                state = self._simulated_ports[port]
                if enabled is not None:
                    state["enabled"] = enabled
                    state["power_enabled"] = enabled
                    state["usb2_data_enabled"] = enabled
                    state["usb3_data_enabled"] = enabled
                if power_enabled is not None:
                    state["power_enabled"] = power_enabled
                if data_enabled is not None:
                    state["usb2_data_enabled"] = data_enabled
                    state["usb3_data_enabled"] = data_enabled
                if usb2_data_enabled is not None:
                    state["usb2_data_enabled"] = usb2_data_enabled
                if usb3_data_enabled is not None:
                    state["usb3_data_enabled"] = usb3_data_enabled
                state["enabled"] = bool(
                    state["power_enabled"]
                    and (state["usb2_data_enabled"] or state["usb3_data_enabled"])
                )
                if current_limit_ma is not None:
                    state["current_limit_a"] = current_limit_ma / 1000
                if port_mode is not None:
                    state["mode"] = port_mode
                return True

            if not BRAINSTEM_AVAILABLE or not self.connected:
                return False

            operations = []
            if enabled is not None:
                operations.append(
                    ("setPortEnable" if enabled else "setPortDisable", (port,))
                )
            if power_enabled is not None:
                operations.append(
                    (
                        "setPowerEnable" if power_enabled else "setPowerDisable",
                        (port,),
                    )
                )
            if data_enabled is not None:
                operations.append(
                    (
                        "setDataEnable" if data_enabled else "setDataDisable",
                        (port,),
                    )
                )
            if usb2_data_enabled is not None:
                operations.append(
                    (
                        "setHiSpeedDataEnable"
                        if usb2_data_enabled
                        else "setHiSpeedDataDisable",
                        (port,),
                    )
                )
            if usb3_data_enabled is not None:
                operations.append(
                    (
                        "setSuperSpeedDataEnable"
                        if usb3_data_enabled
                        else "setSuperSpeedDataDisable",
                        (port,),
                    )
                )
            if current_limit_ma is not None:
                operations.append(
                    ("setPortCurrentLimit", (port, current_limit_ma * 1000))
                )
            if port_mode is not None:
                operations.append(("setPortMode", (port, port_mode)))

            for method_name, args in operations:
                if not self._write_result(self.stem.usb, method_name, *args):
                    return False
            return True

    def set_name(self, name: str) -> bool:
        """Set the hub's controller-level friendly name."""
        name = str(name).strip()
        if not name or len(name) > 128:
            return False
        with self._lock:
            if self.simulate:
                self._simulated_name = name
                return True
            if not BRAINSTEM_AVAILABLE or not self.connected:
                return False
            return self._write_result(self.stem.system, "setName", name)

    def reset_port(
        self,
        port: int,
        *,
        reset_type: str = "power",
        delay_ms: int = 750,
    ) -> bool:
        """Cycle a port's power or data lines and restore their prior state."""
        if (
            not self._valid_port(port)
            or reset_type not in {"power", "data"}
            or not 100 <= delay_ms <= 10_000
        ):
            return False

        with self._lock:
            state = self.get_port_status(port)
            if self.simulate:
                return True
            if not BRAINSTEM_AVAILABLE or not self.connected:
                return False

            if reset_type == "power":
                if not self._write_result(
                    self.stem.usb,
                    "setPowerDisable",
                    port,
                ):
                    return False
                time.sleep(delay_ms / 1000)
                if state.get("power_enabled"):
                    return self._write_result(
                        self.stem.usb,
                        "setPowerEnable",
                        port,
                    )
                return True

            if not self._write_result(
                self.stem.usb,
                "setDataDisable",
                port,
            ):
                return False
            time.sleep(delay_ms / 1000)
            operations = []
            if state.get("usb2_data_enabled"):
                operations.append("setHiSpeedDataEnable")
            if state.get("usb3_data_enabled"):
                operations.append("setSuperSpeedDataEnable")
            return all(
                self._write_result(self.stem.usb, method, port) for method in operations
            )

    def clear_port_errors(self, port: int) -> bool:
        if not self._valid_port(port):
            return False
        with self._lock:
            if self.simulate:
                self._simulated_ports[port]["error"] = False
                self._simulated_ports[port]["errors"] = []
                return True
            if not BRAINSTEM_AVAILABLE or not self.connected:
                return False
            return self._write_result(
                self.stem.usb,
                "clearPortErrorStatus",
                port,
            )

    def disconnect(self):
        with self._lock:
            if self.stem and self.connected:
                self.stem.disconnect()
                self.connected = False
