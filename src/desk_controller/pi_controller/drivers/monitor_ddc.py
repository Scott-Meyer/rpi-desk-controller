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

    STANDARD_FEATURE = "60"
    # Some recent LG displays (e.g. the UltraGear/OLED GX-series) silently
    # ignore writes to the standard MCCS input-select feature for their
    # "alternate" input set (LG-Alt DisplayPort/HDMI). They respond instead
    # on this manufacturer-specific feature code, mirroring the
    # ``ddcAlt``/``inputSelectAlt`` pairing BetterDisplay uses on macOS.
    ALT_FEATURE = "f4"
    # A plain VCP write to 0xF4 on the monitor's normal I2C address (0x51)
    # is silently accepted (ddcutil reports success) but has no visible
    # effect. The write only actually reaches the display's alt-input
    # controller over LG's undocumented "DDC2AB" service side-channel,
    # addressed at 0x50 instead of the usual 0x51. Confirmed against
    # hardware: https://github.com/rockowitz/ddcutil/wiki/Switching-input-source-on-LG-monitors
    ALT_I2C_SOURCE_ADDR = "0x50"

    def __init__(
        self,
        display_id: int = 1,
        simulate: bool = False,
        use_alt_addressing: bool = False,
    ):
        self.display_id = display_id
        self.simulate = simulate
        self.use_alt_addressing = use_alt_addressing
        self._simulated_input_source: Optional[int] = None

    def _feature_code(self) -> str:
        return self.ALT_FEATURE if self.use_alt_addressing else self.STANDARD_FEATURE

    def get_input_source(self) -> Optional[int]:
        """Return the active input-select value, or ``None`` when unavailable."""
        if self.simulate:
            return self._simulated_input_source

        feature = self._feature_code()
        cmd = [
            "ddcutil",
            "--display",
            str(self.display_id),
            "getvcp",
            feature,
            "--terse",
        ]
        if self.use_alt_addressing:
            cmd += ["--i2c-source-addr", self.ALT_I2C_SOURCE_ADDR]
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
                rf"^\s*VCP\s+{feature}\s+SNC\s+[xX]([0-9a-fA-F]{{2}})\s*$",
                result.stdout,
                re.MULTILINE | re.IGNORECASE,
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
        Sets the monitor's input-select VCP feature (0x60, or 0xF4 when
        ``use_alt_addressing`` targets an LG-Alt input).
        Common standard VCP 0x60 codes:
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

        feature = self._feature_code()
        cmd = [
            "ddcutil",
            "--display",
            str(self.display_id),
            "setvcp",
            feature,
            input_hex_code,
        ]
        if self.use_alt_addressing:
            # A plain write to the monitor's normal I2C address (0x51) is
            # silently accepted but never actually switches the input; LG's
            # alt-input controller only listens on the DDC2AB side-channel.
            # Deliberately no --noverify: on this ddcutil build, combining it
            # with --i2c-source-addr causes ddcutil to print a "--verify and
            # --noverify both specified" error while still exiting 0 (i.e.
            # doing nothing but reporting success) - confirmed on hardware.
            cmd += ["--i2c-source-addr", self.ALT_I2C_SOURCE_ADDR]
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
