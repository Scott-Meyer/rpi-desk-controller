"""Exercise the actual SSH deployment guard before any release-owned files change."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "deploy.sh"


@unittest.skipUnless(os.name == "posix", "SSH deployment guard uses POSIX flock")
class DeploymentGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state_dir = self.root / "config" / ".update"
        self.state_dir.mkdir(parents=True)
        self.marker = self.state_dir / "manual-deploy.lock"
        script = SCRIPT.read_text()
        self.guard = script.split("<<'DEPLOY_GUARD'\n", 1)[1].split(
            "\nDEPLOY_GUARD", 1
        )[0]

    def guard_run(self, identifier):
        return subprocess.run(
            ["bash", "-s", "--", str(self.root), identifier],
            input=self.guard,
            text=True,
            capture_output=True,
        )

    def test_guard_is_owned_and_cannot_overwrite_an_existing_deployment(self):
        first = self.guard_run("deployment-one")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.marker.read_text().strip(), "deployment-one")
        second = self.guard_run("deployment-two")
        self.assertNotEqual(second.returncode, 0)
        self.assertEqual(self.marker.read_text().strip(), "deployment-one")

    def test_web_install_in_progress_prevents_ssh_source_sync(self):
        (self.state_dir / "status.json").write_text(json.dumps({"state": "staging"}))
        result = self.guard_run("deployment-one")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())


if __name__ == "__main__":
    unittest.main()
