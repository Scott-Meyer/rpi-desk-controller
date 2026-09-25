import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from desk_controller.pi_controller.release_source import Release, StagedRelease

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rpi_release_stage.py"
spec = importlib.util.spec_from_file_location("rpi_release_stage_test", SCRIPT)
stage_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stage_module)


class ReleaseStageRetryTests(unittest.TestCase):
    def test_failed_dependency_install_does_not_block_retry_of_same_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "releases"
            directory.mkdir()
            commit = "b" * 40
            destination = directory / commit
            release = Release(
                tag="v1.2.1",
                commit=commit,
                release_id=42,
                archive_asset_id=10,
                manifest_asset_id=11,
                release_url="https://github.com/Scott-Meyer/rpi-desk-controller/releases/tag/v1.2.1",
                version="1.2.1",
            )
            stages = []

            def stage(_release, target):
                self.assertFalse(target.exists())
                target.mkdir()
                package = target / "src" / "desk_controller"
                package.mkdir(parents=True)
                (package / "__init__.py").write_text('__version__ = "1.2.1"\n')
                stages.append(target)
                return StagedRelease(release, target)

            calls = []

            def run(command, **_kwargs):
                calls.append(command)
                if len(calls) == 2:
                    raise subprocess.CalledProcessError(1, command)

            with (
                patch.object(
                    stage_module.release_source, "discover_latest", return_value=release
                ),
                patch.object(
                    stage_module.release_source, "stage_release", side_effect=stage
                ),
                patch.object(stage_module.subprocess, "run", side_effect=run),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    stage_module.prepare(
                        release.tag, commit, release.release_id, destination
                    )
                self.assertFalse(
                    destination.exists(), "A failed pip must not leave an unusable slot"
                )
                # Simulate a killed process whose finally block could not run.
                destination.mkdir()
                (destination / "partial-download").write_text("partial")
                stage_module.prepare(
                    release.tag, commit, release.release_id, destination
                )

            self.assertEqual(len(stages), 2)
            self.assertFalse((destination / "partial-download").exists())
            self.assertEqual(len(calls), 5)
            self.assertTrue(
                (destination / "src/desk_controller/_source_version.json").is_file()
            )


if __name__ == "__main__":
    unittest.main()
