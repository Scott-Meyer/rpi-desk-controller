import json
import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from desk_controller.pi_controller.main import DeskControllerApp
from desk_controller.pi_controller.release_source import Release
from desk_controller.pi_controller.release_update import PiReleaseUpdate, UpdateConflict


@unittest.skipUnless(os.name == "posix", "Pi updater requires POSIX filesystem locks")
class PiReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "config").mkdir()
        self.restart = Mock()
        self.updater = PiReleaseUpdate(self.root, self.restart)
        self.release = Release(
            tag="v1.2.1",
            commit="b" * 40,
            release_id=42,
            archive_asset_id=10,
            manifest_asset_id=11,
            release_url="https://github.com/Scott-Meyer/rpi-desk-controller/releases/tag/v1.2.1",
            version="1.2.1",
        )
        self.installed = {"commit": "a" * 40, "dirty": False}

    def test_request_is_atomic_and_cannot_restart_twice(self):
        with (
            patch(
                "desk_controller.pi_controller.release_update.source_version.RUNNING_SOURCE",
                self.installed,
            ),
            patch(
                "desk_controller.pi_controller.release_update.release_source.discover_latest",
                return_value=self.release,
            ),
        ):
            self.assertTrue(self.updater.availability()["available"])
            self.assertEqual(self.updater.request_latest()["status"], "accepted")
            self.restart.assert_called_once()
            state = json.loads(self.updater.status_path.read_text())
            self.assertEqual(
                (state["state"], state["tag"], state["release_id"]),
                ("requested", "v1.2.1", 42),
            )
            with self.assertRaises(UpdateConflict):
                self.updater.request_latest()
            self.restart.assert_called_once()

    def test_admin_credential_and_manual_deployment_guard(self):
        token = self.updater.state_dir / "admin-token"
        token.parent.mkdir(parents=True)
        token.write_text("private-admin-token\n")
        os.chmod(token, 0o600)
        self.assertFalse(self.updater.authenticate("incorrect"))
        self.assertTrue(self.updater.authenticate("private-admin-token"))
        os.chmod(token, 0o644)
        self.assertFalse(self.updater.authenticate("private-admin-token"))
        os.chmod(token, 0o600)
        (self.updater.state_dir / "manual-deploy.lock").touch()
        with (
            patch(
                "desk_controller.pi_controller.release_update.source_version.RUNNING_SOURCE",
                self.installed,
            ),
            patch(
                "desk_controller.pi_controller.release_update.release_source.discover_latest",
                return_value=self.release,
            ),
        ):
            with self.assertRaisesRegex(UpdateConflict, "SSH deployment"):
                self.updater.request_latest()
        self.restart.assert_not_called()

    def test_failed_bootstrap_summary_is_public_but_detailed_error_is_not(self):
        self.updater._write(
            {
                "state": "failed",
                "tag": "v1.2.1",
                "commit": "b" * 40,
                "message": "New release exited before health confirmation; previous installation restored.",
                "error": "private /home/meyer/rpi-desk-controller/config/.update/releases/...",
            }
        )
        status = self.updater.status()
        self.assertIn("previous installation restored", status["message"])
        self.assertNotIn("error", status)
        self.assertNotIn("/home/meyer", str(status))

    def test_health_ack_requires_the_active_release_commit(self):
        self.updater._write(
            {"state": "awaiting_health", "tag": "v1.2.1", "commit": "b" * 40}
        )
        with patch(
            "desk_controller.pi_controller.release_update.source_version.RUNNING_SOURCE",
            self.installed,
        ):
            self.assertFalse(self.updater.mark_healthy())
        with patch(
            "desk_controller.pi_controller.release_update.source_version.RUNNING_SOURCE",
            {"commit": "b" * 40, "dirty": False},
        ):
            self.assertTrue(self.updater.mark_healthy())
        self.assertEqual(self.updater.status()["state"], "completed")


class APIReadinessTests(unittest.TestCase):
    def test_failed_bind_cannot_report_new_release_healthy(self):
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            controller = DeskControllerApp.__new__(DeskControllerApp)
            controller.config = {
                "server": {"host": "127.0.0.1", "port": occupied.getsockname()[1]}
            }
            controller._start_api_server()
            controller._api_thread.join(timeout=5)
            self.assertFalse(controller._api_ready())


if __name__ == "__main__":
    unittest.main()
