#!/usr/bin/env python3
"""Verify and prepare one Pi release outside the running installation.

Invoked by the stable systemd pre-start bootstrap using the *previous* venv.
It never switches the active venv or edits config/config.yaml.
"""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from desk_controller.pi_controller import release_source

_VERSION = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')


def prepare(tag: str, commit: str, release_id: int, destination: Path) -> None:
    release = release_source.discover_latest()
    if (release.tag, release.commit, release.release_id) != (tag, commit, release_id):
        raise release_source.ReleaseIntegrityError(
            "The latest release changed since it was requested; check again"
        )
    destination = Path(destination)
    # An interrupted pip build can leave a partial slot. The bootstrap cannot
    # switch to it until this helper succeeds, so it is safe to discard on retry.
    if (
        destination.parent.name != "releases"
        or destination.name != commit
        or destination.is_symlink()
    ):
        raise ValueError("invalid Pi release staging directory")
    if (destination / "venv").resolve() == Path(sys.prefix).resolve():
        raise ValueError("refusing to replace the active Pi interpreter")
    if destination.exists():
        shutil.rmtree(destination)
    staged = release_source.stage_release(release, destination).destination
    try:
        package_source = staged / "src" / "desk_controller" / "__init__.py"
        version_match = _VERSION.search(package_source.read_text(encoding="utf-8"))
        if not version_match or f"v{version_match.group(1)}" != tag:
            raise release_source.ReleaseIntegrityError(
                "Pi source version does not match release tag"
            )

        stamp = staged / "src" / "desk_controller" / "_source_version.json"
        stamp.write_text(
            json.dumps({"commit": commit, "dirty": False}) + "\n", encoding="utf-8"
        )
        venv = staged / "venv"
        subprocess.run(
            ["/usr/bin/python3", "-m", "venv", str(venv)], check=True, timeout=120
        )
        python = venv / "bin" / "python"
        env = {**os.environ, "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1"}
        subprocess.run(
            [str(python), "-m", "pip", "install", "-e", f"{staged}[pi,acroname]"],
            check=True,
            timeout=480,
            env=env,
        )
        subprocess.run(
            [
                str(python),
                "-c",
                "from desk_controller.pi_controller.main import DeskControllerApp",
            ],
            check=True,
            timeout=30,
            env=env,
        )
    except BaseException:
        shutil.rmtree(staged)
        raise
    print(f"Prepared verified Pi source {tag} ({commit[:12]})", flush=True)


def main() -> int:
    if len(sys.argv) != 5:
        print("Expected tag, commit, release ID and destination", file=sys.stderr)
        return 2
    tag, commit, raw_id, destination = sys.argv[1:]
    try:
        prepare(tag, commit, int(raw_id), Path(destination))
    except (
        ValueError,
        OSError,
        subprocess.SubprocessError,
        release_source.ReleaseSourceError,
    ) as exc:
        print(f"Pi release preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
