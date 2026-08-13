"""Local monitor and USB hardware backends for delegated KVM steps."""

import ctypes
import logging
import os
import re
import subprocess
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any, Dict, Literal, Mapping, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

from desk_controller.core.kvm import KVMHardwareCommand
from desk_controller.pi_controller.drivers.acroname_hub import (
    AcronameHubController,
)
from desk_controller.pi_controller.drivers.monitor_ddc import (
    MonitorDDCController,
)

logger = logging.getLogger(__name__)

_INPUT_PATTERN = re.compile(r"^(?:0x)?[0-9A-Fa-f]{1,2}$")
DEFAULT_BETTERDISPLAY_PATH = (
    "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
)


class DesktopMonitorKVMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    backend: Literal["auto", "betterdisplay", "windows_ddc", "ddcutil"] = "auto"
    display_name: str = Field(default="", max_length=256)
    betterdisplay_path: str = Field(
        default=DEFAULT_BETTERDISPLAY_PATH,
        max_length=2048,
    )
    display_id: int = Field(default=1, ge=1, le=64)
    inputs: Dict[str, str] = Field(
        default_factory=lambda: {"pc1": "0x0f", "pc2": "0x11"}
    )

    @field_validator("display_name", "betterdisplay_path")
    @classmethod
    def strip_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("inputs")
    @classmethod
    def validate_inputs(cls, value: Dict[str, str]) -> Dict[str, str]:
        if set(value) != {"pc1", "pc2"}:
            raise ValueError("monitor inputs must define exactly pc1 and pc2")
        normalized = {}
        for key, raw_input in value.items():
            input_value = str(raw_input).strip()
            if not _INPUT_PATTERN.fullmatch(input_value):
                raise ValueError("monitor inputs must be one-byte hexadecimal values")
            normalized[key] = f"0x{int(input_value, 16):02x}"
        return normalized


class DesktopUSBKVMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    serial_number: Optional[int] = Field(default=None, ge=0)


class DesktopHotkeySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    shortcut: str = Field(default="ctrl+option+command+k", max_length=128)

    @field_validator("shortcut")
    @classmethod
    def validate_shortcut(cls, value: str) -> str:
        from desk_controller.desktop_agent.hotkeys import parse_hotkey

        normalized = value.strip().lower()
        parse_hotkey(normalized)
        return normalized


class DesktopKVMSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hotkey: DesktopHotkeySettings = Field(default_factory=DesktopHotkeySettings)
    monitor: DesktopMonitorKVMSettings = Field(
        default_factory=DesktopMonitorKVMSettings
    )
    usb: DesktopUSBKVMSettings = Field(default_factory=DesktopUSBKVMSettings)


class BetterDisplayMonitorController:
    """Use BetterDisplay's documented CLI integration for direct DDC."""

    def __init__(self, settings: DesktopMonitorKVMSettings):
        self.settings = settings

    def _base_command(self, operation: str) -> list:
        command = [
            self.settings.betterdisplay_path,
            operation,
        ]
        if self.settings.display_name:
            command.append(f"-name={self.settings.display_name}")
        command.extend(
            [
                "-feature=ddc",
                "-vcp=inputSelect",
            ]
        )
        return command

    def get_input_source(self) -> Optional[int]:
        binary = Path(self.settings.betterdisplay_path)
        if not binary.is_file():
            logger.error("BetterDisplay CLI is not installed at %s", binary)
            return None
        try:
            result = subprocess.run(
                [*self._base_command("get"), "-value"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("BetterDisplay monitor read failed")
            return None
        if result.returncode != 0:
            logger.error(
                "BetterDisplay monitor read failed: %s",
                result.stderr.strip() or f"exit {result.returncode}",
            )
            return None
        raw_value = result.stdout.strip().split(",", 1)[0].strip()
        try:
            return int(raw_value, 0)
        except ValueError:
            logger.error(
                "Could not parse BetterDisplay input value: %s",
                result.stdout.strip(),
            )
            return None

    def set_input_source(self, input_code: str) -> bool:
        binary = Path(self.settings.betterdisplay_path)
        if not binary.is_file():
            logger.error("BetterDisplay CLI is not installed at %s", binary)
            return False
        value = int(str(input_code), 16)
        try:
            result = subprocess.run(
                [*self._base_command("set"), f"-value={value}"],
                check=False,
                capture_output=True,
                text=True,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            logger.exception("BetterDisplay monitor write failed")
            return False
        if result.returncode == 0:
            return True
        logger.error(
            "BetterDisplay monitor write failed: %s",
            result.stderr.strip() or f"exit {result.returncode}",
        )
        return False


class WindowsDDCController:
    """Use the Windows physical-monitor API to access VCP input select."""

    VCP_INPUT_SELECT = 0x60

    class PhysicalMonitor(ctypes.Structure):
        _fields_ = [
            ("handle", wintypes.HANDLE),
            ("description", wintypes.WCHAR * 128),
        ]

    def __init__(self, display_name: str = ""):
        self.display_name = display_name.casefold()

    def _with_monitor(self, callback) -> Any:
        if os.name != "nt":
            return None
        user32 = ctypes.windll.user32
        dxva2 = ctypes.windll.dxva2
        monitors = []
        monitor_proc = ctypes.WINFUNCTYPE(
            wintypes.BOOL,
            wintypes.HMONITOR,
            wintypes.HDC,
            ctypes.POINTER(wintypes.RECT),
            wintypes.LPARAM,
        )

        def collect(handle, _hdc, _rect, _data):
            monitors.append(handle)
            return True

        enum_callback = monitor_proc(collect)
        if not user32.EnumDisplayMonitors(
            None,
            None,
            enum_callback,
            0,
        ):
            return None

        for monitor in monitors:
            count = wintypes.DWORD()
            if not dxva2.GetNumberOfPhysicalMonitorsFromHMONITOR(
                monitor,
                ctypes.byref(count),
            ):
                continue
            physical = (self.PhysicalMonitor * count.value)()
            if not dxva2.GetPhysicalMonitorsFromHMONITOR(
                monitor,
                count.value,
                physical,
            ):
                continue
            try:
                for item in physical:
                    if (
                        self.display_name
                        and self.display_name not in item.description.casefold()
                    ):
                        continue
                    return callback(dxva2, item.handle)
            finally:
                for item in physical:
                    dxva2.DestroyPhysicalMonitor(item.handle)
        return None

    def get_input_source(self) -> Optional[int]:
        def read(dxva2, handle):
            current = wintypes.DWORD()
            maximum = wintypes.DWORD()
            if not dxva2.GetVCPFeatureAndVCPFeatureReply(
                handle,
                self.VCP_INPUT_SELECT,
                None,
                ctypes.byref(current),
                ctypes.byref(maximum),
            ):
                return None
            return int(current.value)

        return self._with_monitor(read)

    def set_input_source(self, input_code: str) -> bool:
        value = int(str(input_code), 16)

        def write(dxva2, handle):
            return bool(
                dxva2.SetVCPFeature(
                    handle,
                    self.VCP_INPUT_SELECT,
                    value,
                )
            )

        return bool(self._with_monitor(write))


class DesktopKVMHardware:
    """Execute the small set of KVM operations authorized in local config."""

    def __init__(
        self,
        settings: DesktopKVMSettings,
        os_type: str,
    ):
        self.settings = settings
        self.os_type = os_type.lower()
        self._lock = threading.RLock()
        self._monitor = self._make_monitor()
        self._usb: Optional[AcronameHubController] = None

    @classmethod
    def from_config(
        cls,
        config: Any,
        os_type: str,
    ) -> "DesktopKVMHardware":
        values = config if isinstance(config, Mapping) else {}
        return cls(DesktopKVMSettings.model_validate(values), os_type)

    def _make_monitor(self):
        settings = self.settings.monitor
        backend = settings.backend
        if backend == "auto":
            backend = {
                "darwin": "betterdisplay",
                "windows": "windows_ddc",
            }.get(self.os_type, "ddcutil")
        if backend == "betterdisplay":
            return BetterDisplayMonitorController(settings)
        if backend == "windows_ddc":
            return WindowsDDCController(settings.display_name)
        return MonitorDDCController(display_id=settings.display_id)

    def _ensure_usb(self) -> Optional[AcronameHubController]:
        if not self.settings.usb.enabled:
            return None
        if self._usb is None:
            self._usb = AcronameHubController(
                self.settings.usb.serial_number,
            )
        if not self._usb.connected and not self._usb.connect():
            return None
        return self._usb

    def execute(
        self,
        command: KVMHardwareCommand,
    ) -> Tuple[bool, Optional[int], str]:
        with self._lock:
            if command.operation.startswith("monitor_"):
                if not self.settings.monitor.enabled:
                    return False, None, "Local monitor control is disabled"
                if command.operation == "monitor_get":
                    value = self._monitor.get_input_source()
                    if value is None:
                        return False, None, "Could not read the monitor input"
                    return True, value, ""
                input_code = self.settings.monitor.inputs[f"pc{command.target_pc + 1}"]
                success = self._monitor.set_input_source(input_code)
                return (
                    success,
                    None,
                    "" if success else "Monitor input change failed",
                )

            if not self.settings.usb.enabled:
                return False, None, "Local USB hub control is disabled"
            usb = self._ensure_usb()
            if usb is None:
                return False, None, "Could not connect to the local USB hub"
            success = usb.switch_upstream_channel(command.target_pc)
            return (
                success,
                None,
                "" if success else "USB upstream change failed",
            )

    def reconfigure(self, settings: DesktopKVMSettings) -> None:
        with self._lock:
            if self._usb is not None:
                self._usb.disconnect()
            self.settings = settings
            self._monitor = self._make_monitor()
            self._usb = None

    def close(self) -> None:
        with self._lock:
            if self._usb is not None:
                self._usb.disconnect()
                self._usb = None
