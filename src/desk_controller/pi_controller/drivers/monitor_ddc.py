"""
DDC/CI Display Input Switcher using ddcutil on Linux.
"""

import logging
import re
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


class MonitorDDCController:
    """Controls monitor input source switching using ddcutil over I2C/HDMI/DP."""

    def __init__(self, display_id: int = 1, simulate: bool = False):
        self.display_id = display_id
        self.simulate = simulate
        self._simulated_input_source: Optional[int] = None

    def get_input_source(self) -> Optional[int]:
        """Return the active VCP 0x60 input value, or ``None`` when unavailable."""
        if self.simulate:
            return self._simulated_input_source

        cmd = [
            "ddcutil",
            "--display",
            str(self.display_id),
            "getvcp",
            "60",
            "--terse",
        ]
        try:
            logger.info("Reading monitor input with DDC/CI")
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.info(
                    "Monitor input is unavailable: %s",
                    result.stderr.strip() or "ddcutil returned no value",
                )
                return None

            match = re.search(
                r"^\s*VCP\s+60\s+SNC\s+[xX]([0-9a-fA-F]{2})\s*$",
                result.stdout,
                re.MULTILINE,
            )
            if match is None:
                logger.warning(
                    "Could not parse monitor input from ddcutil output: %s",
                    result.stdout.strip(),
                )
                return None
            return int(match.group(1), 16)
        except FileNotFoundError:
            logger.error("ddcutil command not found; monitor input could not be read")
            return None
        except subprocess.TimeoutExpired:
            logger.info("Monitor did not respond while reading its input")
            return None
        except Exception as e:
            logger.error(f"Error reading monitor input with ddcutil: {e}")
            return None

    def set_input_source(self, input_hex_code: str) -> bool:
        """
        Sets monitor input source VCP feature 0x60.
        Common VCP 0x60 codes:
        - 0x0f: DisplayPort-1
        - 0x10: DisplayPort-2
        - 0x11: HDMI-1
        - 0x12: HDMI-2
        """
        if self.simulate:
            try:
                self._simulated_input_source = int(str(input_hex_code), 16)
            except ValueError:
                logger.error("Invalid simulated monitor input: %s", input_hex_code)
                return False
            logger.info("[Simulation] Monitor input updated to %s", input_hex_code)
            return True

        cmd = [
            "ddcutil",
            "--display",
            str(self.display_id),
            "setvcp",
            "60",
            input_hex_code,
        ]
        try:
            logger.info(f"Running DDC command: {' '.join(cmd)}")
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                logger.info(f"Monitor input updated to {input_hex_code}")
                return True
            else:
                logger.warning(
                    f"ddcutil returned non-zero exit code: {result.stderr.strip()}"
                )
                return False
        except FileNotFoundError:
            logger.error("ddcutil command not found; monitor input was not changed")
            return False
        except Exception as e:
            logger.error(f"Error executing ddcutil: {e}")
            return False
