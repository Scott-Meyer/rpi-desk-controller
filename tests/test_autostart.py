import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_controller.desktop_agent.autostart import AutostartManager


class MacOSAutostartMigrationTests(unittest.TestCase):
    def test_enable_replaces_legacy_launch_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.plist"
            legacy = Path(directory) / "legacy.plist"
            legacy.write_text("legacy", encoding="utf-8")

            with (
                patch(
                    "desk_controller.desktop_agent.autostart.platform.system",
                    return_value="Darwin",
                ),
                patch.object(
                    AutostartManager,
                    "_macos_plist_paths",
                    return_value=(str(current), str(legacy)),
                ),
            ):
                self.assertTrue(AutostartManager.is_enabled())
                self.assertTrue(AutostartManager.enable())

            self.assertTrue(current.is_file())
            self.assertFalse(legacy.exists())
            self.assertIn(
                AutostartManager.MACOS_LABEL,
                current.read_text(encoding="utf-8"),
            )

    def test_disable_removes_current_and_legacy_launch_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            current = Path(directory) / "current.plist"
            legacy = Path(directory) / "legacy.plist"
            current.write_text("current", encoding="utf-8")
            legacy.write_text("legacy", encoding="utf-8")

            with (
                patch(
                    "desk_controller.desktop_agent.autostart.platform.system",
                    return_value="Darwin",
                ),
                patch.object(
                    AutostartManager,
                    "_macos_plist_paths",
                    return_value=(str(current), str(legacy)),
                ),
            ):
                self.assertTrue(AutostartManager.disable())

            self.assertFalse(current.exists())
            self.assertFalse(legacy.exists())


if __name__ == "__main__":
    unittest.main()
