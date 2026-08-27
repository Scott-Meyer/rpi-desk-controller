"""
Desktop Agent System Tray Icon (pystray) for Windows & macOS with Dynamic Audio Device Selector.
"""

import logging
import threading
import webbrowser

from PIL import Image, ImageDraw

from desk_controller.config import (
    config_exists,
    load_config,
    resolve_config_path,
    save_mqtt_config,
    user_config_path,
)
from desk_controller.desktop_agent.agent import DesktopAgent
from desk_controller.desktop_agent.autostart import AutostartManager
from desk_controller.desktop_agent.config_server import (
    DesktopButtonConfigServer,
)
from desk_controller.desktop_agent.hotkeys import GlobalHotkeyManager
from desk_controller.desktop_agent.settings import show_mqtt_settings
from desk_controller.desktop_agent.updater import GitHubReleaseUpdater

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("TrayApp")


def _load_pystray():
    """Load the platform tray backend only when the application needs it."""
    import pystray

    return pystray


def create_tray_icon(connected: bool = False):
    """Generate a status-colored antialiased icon for the system tray."""
    width, height = 64, 64
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    status_color = (52, 199, 123) if connected else (235, 155, 52)
    draw.ellipse([4, 4, 60, 60], fill=status_color)
    draw.arc([16, 16, 48, 48], start=180, end=360, fill=(255, 255, 255), width=5)
    draw.rectangle([14, 30, 22, 46], fill=(255, 255, 255))
    draw.rectangle([42, 30, 50, 46], fill=(255, 255, 255))
    if not connected:
        draw.line([13, 13, 51, 51], fill=(105, 70, 25), width=6)
    return img


class DeskAgentTrayApp:
    def __init__(self):
        self.agent = DesktopAgent()
        self.autostart = AutostartManager()
        self.updater = GitHubReleaseUpdater()
        self.icon = None
        self._mqtt_connected = False
        self.hotkey_manager = None
        resolved_path = resolve_config_path()
        self.config_path = (
            resolved_path if resolved_path.is_file() else user_config_path()
        )
        self.config_server = DesktopButtonConfigServer(
            self.agent,
            self.config_path,
        )

    def _on_select_audio(self, device_id: str, device_name: str):
        logger.info(f"Tray menu selected audio device: {device_name} ({device_id})")
        if self.agent and self.agent.audio_driver:
            if self.agent.audio_driver.set_output_device(device_id):
                self.agent.publish_state()
            else:
                logger.error("Audio output did not change to %s", device_name)

    def _refresh_audio(self, icon, item):
        """Force pystray to rebuild its dynamic menus from current OS state."""
        logger.info("Refreshing audio output devices...")

    def _notify(self, message: str) -> None:
        if not self.icon:
            return
        try:
            self.icon.notify(message, "Desk Agent")
        except Exception:
            logger.exception("Could not display desktop notification")

    def _connection_menu_text(self, item=None) -> str:
        if getattr(self, "_mqtt_connected", False):
            return "● MQTT connected"
        if self.agent and getattr(self.agent.mqtt, "is_auth_failed", False):
            return "● MQTT auth failed (bad credentials)"
        return "● MQTT offline — reconnecting"

    def _on_connection_state_changed(self, connected: bool) -> None:
        """Keep the tray icon, tooltip, and menu aligned with MQTT state."""
        self._mqtt_connected = bool(connected)
        icon = getattr(self, "icon", None)
        if icon is None:
            return
        is_auth_fail = bool(self.agent and getattr(self.agent.mqtt, "is_auth_failed", False))
        icon.icon = create_tray_icon(self._mqtt_connected)
        if self._mqtt_connected:
            icon.title = "Desk Agent — MQTT connected"
        elif is_auth_fail:
            icon.title = "Desk Agent — MQTT authentication failed"
            self._notify("MQTT authentication failed: bad username or password.")
        else:
            icon.title = "Desk Agent — MQTT offline, reconnecting"
        try:
            icon.update_menu()
        except Exception:
            logger.exception("Could not refresh tray connection status")

    def _edit_mqtt_settings(self, icon=None, item=None):
        config_server = getattr(self, "config_server", None)
        if config_server is not None:
            try:
                url = config_server.start()
                webbrowser.open(f"{url}#mqtt")
                return
            except OSError as exc:
                logger.warning("Could not open web editor for MQTT settings: %s", exc)

        current = load_config(
            str(self.config_path) if self.config_path.is_file() else None
        )["mqtt"]
        status_info = {
            "connected": bool(self.agent.mqtt.is_connected) if self.agent else False,
            "auth_failed": getattr(self.agent.mqtt, "is_auth_failed", False) if self.agent else False,
            "auth_error": getattr(self.agent.mqtt, "auth_error", None) if self.agent else None,
        }
        settings = show_mqtt_settings(current, status_info=status_info)
        if settings is None:
            return

        try:
            saved_path = save_mqtt_config(settings, self.config_path)
        except (OSError, ValueError) as exc:
            logger.error("Could not save MQTT settings: %s", exc)
            self._notify(f"Could not save MQTT settings: {exc}")
            return

        self.config_path = saved_path
        if config_server is not None:
            config_server.update_config_path(saved_path)
        if self.agent.reconfigure_mqtt(settings):
            logger.info("MQTT settings saved to %s; reconnecting", saved_path)
            self._notify("MQTT settings saved. Connecting to the broker…")
        else:
            logger.error("MQTT settings were saved, but reconnection could not start")
            self._notify("Settings saved, but the MQTT connection could not start.")

    def _open_button_config(self, icon=None, item=None):
        try:
            url = self.config_server.start()
            webbrowser.open(url)
        except OSError as exc:
            logger.error("Could not open button editor: %s", exc)
            self._notify(f"Could not open button editor: {exc}")

    def _toggle_autostart(self, icon, item):
        current_state = self.autostart.is_enabled()
        if current_state:
            self.autostart.disable()
        else:
            self.autostart.enable()

    def _apply_hotkey_settings(self, settings) -> None:
        if self.hotkey_manager is None:
            return
        try:
            self.hotkey_manager.configure(
                settings.enabled,
                settings.shortcut,
            )
        except (OSError, ValueError) as exc:
            logger.error("Could not register global KVM shortcut: %s", exc)
            self._notify(f"Could not register the KVM shortcut: {exc}")

    def _check_updates(self, icon, item):
        def do_check():
            logger.info("Checking for GitHub release updates...")
            try:
                info = self.updater.check_for_updates()
                if info.get("update_available"):
                    latest_ver = info.get("latest_version")
                    logger.info("New update found: %s", latest_ver)
                    self._notify(
                        f"Desk Agent {latest_ver} is available. "
                        "Reviewing the release in your browser…"
                    )
                    if latest_ver:
                        self.updater.open_release(latest_ver)
                else:
                    logger.info("Application is up to date!")
                    self._notify("Your Desk Agent is up to date!")
            except Exception as exc:
                logger.error("Failed to check for updates: %s", exc)
                self._notify("Could not complete update check.")

        threading.Thread(target=do_check, name="update-checker", daemon=True).start()

    def _on_exit(self, icon, item):
        logger.info("Exiting Desktop Agent...")
        config_server = getattr(self, "config_server", None)
        if config_server is not None:
            config_server.stop()
        hotkey_manager = getattr(self, "hotkey_manager", None)
        if hotkey_manager is not None:
            hotkey_manager.stop()
        if self.agent:
            self.agent.set_connection_state_callback(None)
            self.agent.stop()
        icon.stop()

    def _make_audio_action(self, dev_id: str, dev_name: str):
        def action(icon, item):
            self._on_select_audio(dev_id, dev_name)

        return action

    def _make_audio_checked(self, is_default: bool):
        def checked(item):
            return is_default

        return checked

    def _build_audio_items(self):
        """Build the audio submenu from a fresh OS query."""
        pystray = _load_pystray()
        audio_items = []
        if self.agent and self.agent.audio_driver:
            state = self.agent.audio_driver.get_audio_state()
            for dev in state.available_devices:
                audio_items.append(
                    pystray.MenuItem(
                        dev.name,
                        self._make_audio_action(dev.id, dev.name),
                        checked=self._make_audio_checked(dev.is_default),
                    )
                )

        if not audio_items:
            audio_items.append(
                pystray.MenuItem(
                    "No Audio Devices Found", lambda icon, item: None, enabled=False
                )
            )

        return tuple(audio_items)

    def build_menu(self):
        """Build a tray menu whose audio submenu is evaluated on every refresh."""
        pystray = _load_pystray()
        return pystray.Menu(
            pystray.MenuItem(
                self._connection_menu_text,
                lambda icon, item: None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Audio Output Device",
                pystray.Menu(self._build_audio_items),
            ),
            pystray.MenuItem("Refresh Audio Devices", self._refresh_audio),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Configure Stream Deck Buttons…",
                self._open_button_config,
            ),
            pystray.MenuItem("MQTT Settings…", self._edit_mqtt_settings),
            pystray.MenuItem(
                "Start on Boot",
                self._toggle_autostart,
                checked=lambda item: self.autostart.is_enabled(),
            ),
            pystray.MenuItem("Check for Updates", self._check_updates),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_exit),
        )

    def run(self):
        pystray = _load_pystray()
        self.config_server.start()
        if not config_exists():
            try:
                webbrowser.open(f"{self.config_server.url}#mqtt")
            except Exception:
                self._edit_mqtt_settings()

        agent_thread = threading.Thread(target=self.agent.run, daemon=True)
        agent_thread.start()

        self._mqtt_connected = bool(self.agent.mqtt.is_connected)
        icon_img = create_tray_icon(self._mqtt_connected)
        self.icon = pystray.Icon(
            "DeskAgent",
            icon_img,
            (
                "Desk Agent — MQTT connected"
                if self._mqtt_connected
                else "Desk Agent — MQTT offline, reconnecting"
            ),
            menu=self.build_menu(),
        )
        self.agent.set_connection_state_callback(
            self._on_connection_state_changed,
        )
        self.hotkey_manager = GlobalHotkeyManager(
            self.agent.request_kvm_toggle,
        )
        self.agent.set_hotkey_reconfigure_callback(
            self._apply_hotkey_settings,
        )
        self._apply_hotkey_settings(
            self.agent.kvm_hardware.settings.hotkey,
        )
        self.icon.run()


def main():
    app = DeskAgentTrayApp()
    app.run()


if __name__ == "__main__":
    main()
