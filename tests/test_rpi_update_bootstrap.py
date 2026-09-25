"""Exercise the actual ExecStartPre program against disposable checkout layouts."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BOOTSTRAP = Path(__file__).resolve().parents[1] / "scripts" / "rpi_update_bootstrap.py"
FIRST = "a" * 40
SECOND = "b" * 40


@unittest.skipUnless(
    os.name == "posix", "Pi bootstrap uses POSIX symlinks and directory fsync"
)
class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "controller"
        self.root.mkdir()
        self.status_path = self.root / "config" / ".update" / "status.json"
        self.status_path.parent.mkdir(parents=True)
        self.venv = self.root / "venv"
        self.venv.mkdir()
        (self.venv / "bin").mkdir()
        (self.venv / "bin" / "python").symlink_to(sys.executable)
        script = self.root / "scripts" / "rpi_release_stage.py"
        script.parent.mkdir()
        script.write_text(
            f"""import pathlib, sys
pathlib.Path('stage_calls').open('a').write(' '.join(sys.argv[1:4]) + '\\n')
if pathlib.Path('fail_stage').exists():
    sys.exit(3)
destination = pathlib.Path(sys.argv[4])
(destination / 'venv' / 'bin').mkdir(parents=True)
(destination / 'venv' / 'bin' / 'python').symlink_to({sys.executable!r})
"""
        )

    def status(self, state="requested", commit=FIRST, **fields):
        self.status_path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "state": state,
                    "tag": "v1.2.3",
                    "commit": commit,
                    "release_id": 24,
                    **fields,
                }
            )
        )

    def boot(self, success=True):
        result = subprocess.run(
            [sys.executable, str(BOOTSTRAP), str(self.root)],
            capture_output=True,
            text=True,
        )
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        return result

    def recorded(self):
        return json.loads(self.status_path.read_text())

    def test_first_upgrade_and_unhealthy_restart_restore_original(self):
        self.status()
        self.boot()
        self.assertEqual(
            (self.root / "stage_calls").read_text(), "v1.2.3 " + FIRST + " 24\n"
        )
        self.assertEqual(self.recorded()["state"], "awaiting_health")
        self.assertTrue(self.venv.is_symlink())
        self.assertEqual(
            self.venv.resolve(), self.status_path.parent / "releases" / FIRST / "venv"
        )
        baseline = self.status_path.parent / "baseline" / "venv"
        self.assertTrue((baseline / "bin" / "python").is_file())

        self.boot()
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertIn("health confirmation", self.recorded()["message"])
        self.assertEqual(self.venv.resolve(), baseline)
        self.assertTrue((self.venv / "bin" / "python").is_file())
        self.assertEqual((self.root / "stage_calls").read_text().count("\n"), 1)
        self.boot()  # A failed update must not retry on every service restart.
        self.assertEqual(self.recorded()["state"], "failed")

    def test_completed_update_then_crash_during_next_update_restores_prior_link(self):
        self.status()
        self.boot()
        original_link = os.readlink(self.venv)
        self.status("completed")
        self.boot()
        self.assertEqual(os.readlink(self.venv), original_link)

        self.status(commit=SECOND)
        self.boot()
        self.assertEqual(
            self.venv.resolve(), self.status_path.parent / "releases" / SECOND / "venv"
        )
        self.assertEqual(
            self.recorded()["previous_venv"],
            {"kind": "symlink", "target": original_link},
        )
        self.boot()
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertEqual(os.readlink(self.venv), original_link)

    def test_interrupted_stage_does_not_repeat_install_on_next_service_start(self):
        self.status("staging")
        self.boot()
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertIn("interrupted during preparation", self.recorded()["message"])
        self.assertTrue((self.venv / "bin" / "python").is_file())
        self.assertFalse((self.root / "stage_calls").exists())

    def test_stage_failure_preserves_running_environment_and_config(self):
        config = self.root / "config" / "config.yaml"
        config.write_text("credentials: preserved\n")
        (self.root / "fail_stage").touch()
        self.status()
        self.boot()
        self.assertFalse(self.venv.is_symlink())
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertIn("release staging failed", self.recorded()["error"])
        self.assertIn("Could not prepare release", self.recorded()["message"])
        self.assertNotIn(str(self.root), self.recorded()["message"])
        self.assertEqual(config.read_text(), "credentials: preserved\n")
        self.assertFalse((self.status_path.parent / "baseline").exists())

    def test_power_loss_during_directory_move_is_recovered_at_either_boundary(self):
        previous = {"kind": "directory"}
        self.status("switching", previous_venv=previous)
        self.boot()  # The original directory was not moved yet.
        self.assertFalse(self.venv.is_symlink())
        self.assertEqual(self.recorded()["state"], "failed")

        baseline = self.status_path.parent / "baseline" / "venv"
        baseline.parent.mkdir()
        self.venv.rename(baseline)  # Power loss after move but before symlink.
        self.status("switching", previous_venv=previous)
        self.boot()
        self.assertEqual(self.venv.resolve(), baseline)
        self.assertEqual(self.recorded()["state"], "failed")

    def test_interrupted_after_link_switch_restores_prior_interpreter(self):
        self.status()
        self.boot()
        self.assertEqual(self.recorded()["state"], "awaiting_health")
        self.status("switching", previous_venv={"kind": "directory"})
        self.boot()
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertEqual(
            self.venv.resolve(), self.status_path.parent / "baseline" / "venv"
        )

    def test_missing_rollback_target_blocks_start_instead_of_booting_new_release(self):
        self.status()
        self.boot()
        baseline = self.status_path.parent / "baseline" / "venv"
        (baseline / "bin" / "python").unlink()
        error = self.boot(success=False)
        self.assertIn("baseline interpreter is unavailable", error.stderr)
        self.assertEqual(self.recorded()["state"], "awaiting_health")
        self.assertNotEqual(self.venv.resolve(), baseline)

    def test_invalid_release_identity_never_runs_stage_or_switches_venv(self):
        self.status(commit="../../elsewhere")
        self.boot()
        self.assertEqual(self.recorded()["state"], "failed")
        self.assertFalse(self.venv.is_symlink())
        self.assertFalse((self.root / "stage_calls").exists())


if __name__ == "__main__":
    unittest.main()
