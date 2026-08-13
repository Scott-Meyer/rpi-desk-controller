"""Install the repository's udev rules when repairing an existing Pi setup."""

import os
import shutil
import subprocess
from pathlib import Path

if os.geteuid() != 0:
    raise SystemExit("Run this script with sudo.")

project_dir = Path(__file__).resolve().parent.parent
source = project_dir / "systemd" / "99-desk-controller.rules"
destination = Path("/etc/udev/rules.d/99-desk-controller.rules")

shutil.copyfile(source, destination)
destination.chmod(0o644)
subprocess.run(["udevadm", "control", "--reload-rules"], check=True)
subprocess.run(["udevadm", "trigger"], check=True)
print(f"Installed udev rules from {source}")
