"""
Autostart manager for Windows (Registry Run key) and macOS (LaunchAgent).
"""

import logging
import os
import platform
import sys

logger = logging.getLogger(__name__)


class AutostartManager:
    APP_NAME = "RPiDeskAgent"
    MACOS_LABEL = "io.github.rpi-desk-controller.agent"
    LEGACY_MACOS_LABEL = "com.scott.rpideskagent"

    @classmethod
    def _macos_plist_paths(cls):
        launch_agents = os.path.expanduser("~/Library/LaunchAgents")
        return (
            os.path.join(launch_agents, f"{cls.MACOS_LABEL}.plist"),
            os.path.join(launch_agents, f"{cls.LEGACY_MACOS_LABEL}.plist"),
        )

    @staticmethod
    def is_enabled() -> bool:
        system = platform.system().lower()
        if system == "windows":
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0,
                    winreg.KEY_READ,
                )
                _, _ = winreg.QueryValueEx(key, AutostartManager.APP_NAME)
                winreg.CloseKey(key)
                return True
            except FileNotFoundError:
                return False
            except Exception as e:
                logger.error(f"Error checking Windows autostart: {e}")
                return False
        elif system == "darwin":
            return any(
                os.path.exists(path) for path in AutostartManager._macos_plist_paths()
            )
        return False

    @staticmethod
    def enable() -> bool:
        system = platform.system().lower()
        exe_path = os.path.abspath(sys.argv[0])
        if system == "windows":
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0,
                    winreg.KEY_SET_VALUE,
                )
                winreg.SetValueEx(
                    key, AutostartManager.APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"'
                )
                winreg.CloseKey(key)
                logger.info(f"Enabled Windows autostart for {exe_path}")
                return True
            except Exception as e:
                logger.error(f"Failed enabling Windows autostart: {e}")
                return False
        elif system == "darwin":
            plist_path, legacy_path = AutostartManager._macos_plist_paths()
            plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{AutostartManager.MACOS_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{exe_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
"""
            try:
                os.makedirs(os.path.dirname(plist_path), exist_ok=True)
                with open(plist_path, "w") as f:
                    f.write(plist_content)
                if os.path.exists(legacy_path):
                    os.remove(legacy_path)
                logger.info(f"Enabled macOS autostart via LaunchAgent: {plist_path}")
                return True
            except Exception as e:
                logger.error(f"Failed enabling macOS autostart: {e}")
                return False
        return False

    @staticmethod
    def disable() -> bool:
        system = platform.system().lower()
        if system == "windows":
            try:
                import winreg

                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run",
                    0,
                    winreg.KEY_SET_VALUE,
                )
                winreg.DeleteValue(key, AutostartManager.APP_NAME)
                winreg.CloseKey(key)
                logger.info("Disabled Windows autostart")
                return True
            except FileNotFoundError:
                return True
            except Exception as e:
                logger.error(f"Failed disabling Windows autostart: {e}")
                return False
        elif system == "darwin":
            for plist_path in AutostartManager._macos_plist_paths():
                if os.path.exists(plist_path):
                    os.remove(plist_path)
            return True
        return False
