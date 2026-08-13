"""Cross-platform observation of a configured USB sentinel device."""

import logging
import plistlib
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from desk_controller.core.usb_observation import USBSentinelConfig

logger = logging.getLogger(__name__)

_WINDOWS_USB_ID = re.compile(
    (
        r"^USB\\VID_([0-9A-F]{4})&PID_([0-9A-F]{4})"
        r"(?:&[^\\]+)*(?:\\(.+))?$"
    ),
    re.IGNORECASE,
)


class USBPresenceProbe:
    """Determine whether one VID/PID/serial tuple is present locally."""

    def __init__(self, os_type: str):
        self.os_type = os_type.lower()

    @staticmethod
    def _matches(
        config: USBSentinelConfig,
        vendor_id: Any,
        product_id: Any,
        serial_number: Any = None,
    ) -> bool:
        try:
            vendor = int(vendor_id)
            product = int(product_id)
        except (TypeError, ValueError):
            return False
        if vendor != config.vendor_id or product != config.product_id:
            return False
        if config.serial_number is None:
            return True
        return str(serial_number or "").strip() == config.serial_number

    @staticmethod
    def _walk_plist(value: Any) -> Iterable[Mapping[str, Any]]:
        if isinstance(value, Mapping):
            yield value
            for child in value.values():
                yield from USBPresenceProbe._walk_plist(child)
        elif isinstance(value, list):
            for child in value:
                yield from USBPresenceProbe._walk_plist(child)

    def _macos_present(self, config: USBSentinelConfig) -> bool:
        result = subprocess.run(
            ["ioreg", "-a", "-p", "IOUSB", "-l", "-w", "0"],
            check=False,
            capture_output=True,
            timeout=8,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or f"ioreg exited with {result.returncode}"
            )
        tree = plistlib.loads(result.stdout)
        return any(
            self._matches(
                config,
                node.get("idVendor"),
                node.get("idProduct"),
                node.get("USB Serial Number"),
            )
            for node in self._walk_plist(tree)
        )

    def _windows_present(self, config: USBSentinelConfig) -> bool:
        instance_pattern = (
            f"USB\\VID_{config.vendor_id:04X}&PID_{config.product_id:04X}*"
        )
        script = (
            "Get-PnpDevice -PresentOnly "
            f"-InstanceId '{instance_pattern}' "
            "-ErrorAction SilentlyContinue | "
            "ForEach-Object { $_.InstanceId }"
        )
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            raise RuntimeError(
                result.stderr.strip() or f"PowerShell exited with {result.returncode}"
            )
        for raw_line in result.stdout.splitlines():
            match = _WINDOWS_USB_ID.match(raw_line.strip())
            if not match:
                continue
            if self._matches(
                config,
                int(match.group(1), 16),
                int(match.group(2), 16),
                match.group(3),
            ):
                return True
        return False

    def _linux_present(self, config: USBSentinelConfig) -> bool:
        for vendor_path in Path("/sys/bus/usb/devices").glob("*/idVendor"):
            try:
                vendor = int(vendor_path.read_text().strip(), 16)
                product = int(
                    vendor_path.with_name("idProduct").read_text().strip(),
                    16,
                )
                serial_path = vendor_path.with_name("serial")
                serial = (
                    serial_path.read_text().strip() if serial_path.is_file() else None
                )
            except (OSError, ValueError):
                continue
            if self._matches(config, vendor, product, serial):
                return True
        return False

    def is_present(self, config: USBSentinelConfig) -> Optional[bool]:
        if not config.is_complete():
            return False
        try:
            if self.os_type == "darwin":
                return self._macos_present(config)
            if self.os_type == "windows":
                return self._windows_present(config)
            if self.os_type == "linux":
                return self._linux_present(config)
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
            logger.exception("USB sentinel observation failed")
            return None
        logger.warning(
            "USB sentinel observation is unsupported on %s",
            self.os_type,
        )
        return None
