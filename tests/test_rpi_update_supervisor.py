"""A hung candidate cannot prevent the stable service parent from restoring it."""

import importlib.util
import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

SUPERVISOR = (
    Path(__file__).resolve().parents[1] / "scripts" / "rpi_update_supervisor.py"
)
BOOTSTRAP = SUPERVISOR.with_name("rpi_update_bootstrap.py")


def load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "posix", "Pi supervisor requires POSIX interpreters")
class SupervisorTests(unittest.TestCase):
    def test_hung_candidate_before_app_import_times_out_and_rolls_back(self):
        supervisor = load_script("rpi_supervisor_test", SUPERVISOR)
        bootstrap = load_script("rpi_bootstrap_test", BOOTSTRAP)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            updates = root / "config" / ".update"
            baseline = updates / "baseline" / "venv" / "bin"
            baseline.mkdir(parents=True)
            (baseline / "python").symlink_to(sys.executable)
            candidate = updates / "releases" / ("b" * 40) / "venv" / "bin"
            candidate.mkdir(parents=True)
            interpreter = candidate / "python"
            interpreter.write_text(
                "#!/bin/sh\nexec "
                + shlex.quote(sys.executable)
                + " -c 'import time; time.sleep(30)'\n"
            )
            interpreter.chmod(0o700)
            (root / "venv").symlink_to(candidate.parent)
            status_path = updates / "status.json"
            status_path.write_text(
                json.dumps(
                    {
                        "schema": 1,
                        "state": "awaiting_health",
                        "tag": "v1.2.1",
                        "commit": "b" * 40,
                        "release_id": 42,
                        "previous_venv": {"kind": "directory"},
                    }
                )
            )

            self.assertEqual(supervisor.run(root, health_timeout=0.5), 1)
            self.assertEqual(
                json.loads(status_path.read_text())["state"], "awaiting_health"
            )
            bootstrap.run(root)
            self.assertEqual(json.loads(status_path.read_text())["state"], "failed")
            self.assertEqual((root / "venv").resolve(), baseline.parent.resolve())
            self.assertTrue((root / "venv/bin/python").is_file())


if __name__ == "__main__":
    unittest.main()
