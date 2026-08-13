"""USB host-switch backends and configuration factory."""

import logging
import threading
import time
from typing import Any, Callable, Dict, Mapping, Optional

from desk_controller.pi_controller.drivers.acroname_hub import (
    AcronameHubController,
)

logger = logging.getLogger(__name__)

try:
    from gpiozero import OutputDevice

    GPIOZERO_AVAILABLE = True
except ImportError:
    OutputDevice = None
    GPIOZERO_AVAILABLE = False


class GPIOToggleUSBController:
    """Drive a toggle-only USB sharing switch through a momentary contact.

    This backend is intended for switches such as the UGREEN CM691/25164.
    A GPIO-controlled isolated relay or optocoupler must be wired in parallel
    with the switch's desktop-controller button. The GPIO must never be wired
    directly to the USB switch's control-port conductors.

    The hardware supplies no channel feedback, so ``active_channel`` is an
    assumed state. Manual button presses while the service is running will
    make that state inaccurate.
    """

    PORT_COUNT = 4
    MAX_CURRENT_LIMIT_MA = 0
    DRIVER = "ugreen_cm691_gpio"
    MANUFACTURER = "UGREEN"
    MODEL = "CM691 / 25164"
    DISPLAY_NAME = "UGREEN USB-C Sharing Switch"
    SUPPORTS_PORT_CONTROL = False
    SUPPORTED_COMMANDS = ()

    def __init__(
        self,
        gpio_pin: int,
        *,
        active_high: bool = False,
        pulse_ms: int = 200,
        initial_channel: int = 0,
        simulate: bool = False,
        output_factory: Optional[Callable[..., Any]] = None,
    ):
        if not 2 <= gpio_pin <= 27:
            raise ValueError("UGREEN relay GPIO must be a BCM pin from 2 to 27")
        if not 50 <= pulse_ms <= 2000:
            raise ValueError("UGREEN relay pulse must be between 50 and 2000 ms")
        if initial_channel not in (0, 1):
            raise ValueError("initial USB channel must be 0 or 1")

        self.gpio_pin = gpio_pin
        self.active_high = active_high
        self.pulse_ms = pulse_ms
        self.active_channel = initial_channel
        self.simulate = simulate
        self.connected = False
        self._output = None
        self._output_factory = output_factory
        self._lock = threading.RLock()

    def connect(self) -> bool:
        with self._lock:
            if self.connected:
                return True
            if self.simulate:
                logger.info(
                    "[Simulation] Connected to UGREEN GPIO switch on BCM %s",
                    self.gpio_pin,
                )
                self.connected = True
                return True

            output_factory = self._output_factory
            if output_factory is None:
                if not GPIOZERO_AVAILABLE:
                    logger.error(
                        "Cannot control the UGREEN switch: gpiozero is not installed"
                    )
                    return False
                output_factory = OutputDevice

            try:
                self._output = output_factory(
                    self.gpio_pin,
                    active_high=self.active_high,
                    initial_value=False,
                )
                self.connected = True
                logger.info(
                    "Connected UGREEN desktop-button relay on BCM %s",
                    self.gpio_pin,
                )
                return True
            except Exception:
                logger.exception(
                    "Could not initialize UGREEN relay GPIO BCM %s",
                    self.gpio_pin,
                )
                self._output = None
                return False

    def switch_upstream_channel(self, channel: int) -> bool:
        """Pulse the button once when the requested channel differs."""
        if channel not in (0, 1):
            logger.error("Invalid UGREEN upstream channel: %s", channel)
            return False

        with self._lock:
            if not self.connected:
                logger.error("Cannot switch UGREEN channel: GPIO is not connected")
                return False
            if channel == self.active_channel:
                return True
            if self.simulate:
                self.active_channel = channel
                logger.info("[Simulation] UGREEN switched to channel %s", channel)
                return True

            try:
                self._output.on()
                time.sleep(self.pulse_ms / 1000)
            except Exception:
                logger.exception("Failed pulsing the UGREEN desktop-button relay")
                return False
            finally:
                try:
                    if self._output is not None:
                        self._output.off()
                except Exception:
                    logger.exception("Failed releasing the UGREEN relay output")
                    return False

            self.active_channel = channel
            logger.info("UGREEN switch toggled to assumed channel %s", channel)
            return True

    def get_hub_status(
        self,
        port_names: Optional[Mapping[int, str]] = None,
    ) -> Dict[str, Any]:
        names = port_names or {}
        ports = [
            {
                "index": port,
                "name": names.get(port, f"USB Port {port}"),
                "available": False,
                "controllable": False,
            }
            for port in range(self.PORT_COUNT)
        ]
        return {
            "connected": self.connected,
            "driver": self.DRIVER,
            "manufacturer": self.MANUFACTURER,
            "model": self.MODEL,
            "name": self.DISPLAY_NAME,
            "serial_number": None,
            "firmware_version": None,
            "hardware_version": None,
            "input_voltage_v": None,
            "input_current_a": None,
            "input_power_w": None,
            "temperature_c": None,
            "uptime_seconds": None,
            "active_upstream": self.active_channel,
            "state_feedback": False,
            "capabilities": {
                "upstream_switch": True,
                "per_port_control": False,
                "telemetry": False,
                "state_feedback": False,
            },
            "ports": ports,
        }

    def set_name(self, _name: str) -> bool:
        return False

    def set_port_settings(self, _port: int, **_settings: Any) -> bool:
        return False

    def reset_port(
        self,
        _port: int,
        *,
        reset_type: str = "power",
        delay_ms: int = 750,
    ) -> bool:
        return False

    def clear_port_errors(self, _port: int) -> bool:
        return False

    def disconnect(self) -> None:
        with self._lock:
            if self._output is not None:
                try:
                    self._output.off()
                    self._output.close()
                except Exception:
                    logger.exception("Failed closing the UGREEN relay GPIO")
            self._output = None
            self.connected = False


def create_usb_controller(
    config: Mapping[str, Any],
    *,
    simulate: bool = False,
) -> Any:
    """Create the configured Pi-side USB host-switch controller.

    Existing configurations containing only ``acroname`` remain valid.
    """
    switch_config = config.get("usb_switch", {})
    if not isinstance(switch_config, Mapping):
        raise ValueError("usb_switch configuration must be a mapping")
    legacy_acroname = config.get("acroname", {})
    if not isinstance(legacy_acroname, Mapping):
        legacy_acroname = {}

    driver = str(switch_config.get("driver", "acroname")).strip().lower()
    if driver == "acroname":
        serial_number = switch_config.get(
            "serial_number",
            legacy_acroname.get("serial_number"),
        )
        return AcronameHubController(
            serial_number,
            simulate=simulate,
        )

    if driver in {"ugreen_cm691_gpio", "ugreen_gpio", "gpio_toggle"}:
        initial_channel = switch_config.get(
            "default_channel",
            legacy_acroname.get("default_channel", 0),
        )
        return GPIOToggleUSBController(
            gpio_pin=int(switch_config.get("gpio_pin", 17)),
            active_high=bool(switch_config.get("gpio_active_high", False)),
            pulse_ms=int(switch_config.get("gpio_pulse_ms", 200)),
            initial_channel=int(initial_channel),
            simulate=simulate,
        )

    raise ValueError("usb_switch.driver must be 'acroname' or 'ugreen_cm691_gpio'")
