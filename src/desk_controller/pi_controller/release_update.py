"""Pi web update requests and durable restart handoff.

The web service chooses a fixed GitHub Release and records intent. It never
installs code itself: systemd runs the stable pre-start installer after the
controller exits, and starts either the verified new version or the old one.
"""

import hmac
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from packaging.version import Version

try:
    import fcntl  # POSIX-only; the desktop agent and Windows CI import the web module too.
except ImportError:
    fcntl = None

from desk_controller import __version__, source_version
from desk_controller.pi_controller import release_source

_ACTIVE_STATES = {"requested", "staging", "switching", "awaiting_health"}


class UpdateConflict(Exception):
    """An update is already active or the installed source is not safe to replace."""


class PiReleaseUpdate:
    """Server-owned release selection, request/status storage and health ack."""

    def __init__(self, project_root: Path, restart: Callable[[], None]):
        self.root = Path(project_root).resolve()
        self.restart = restart
        self.state_dir = self.root / "config" / ".update"
        self.status_path = self.state_dir / "status.json"
        self.lock_path = self.state_dir / "install.lock"

    @contextmanager
    def _lock(self) -> Iterator[None]:
        if fcntl is None:
            raise OSError("Pi release installation requires POSIX file locks")
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict[str, Any]:
        if not self.status_path.exists():
            return {"state": "idle", "message": "No update has been requested."}
        data = json.loads(self.status_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != 1:
            raise UpdateConflict(
                "Unknown update status format; manual recovery required"
            )
        return data

    def _write(self, state: dict[str, Any]) -> None:
        state = {
            **state,
            "schema": 1,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".status-", dir=self.state_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(state, out)
                out.write("\n")
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.status_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def authenticate(self, credential: str) -> bool:
        """Allow an SSH-provisioned secret, never a setting editable in the LAN UI."""
        if not isinstance(credential, str) or not 1 <= len(credential) <= 200:
            return False
        path = self.state_dir / "admin-token"
        try:
            if path.is_symlink() or path.stat().st_mode & 0o077:
                return False
            expected = path.read_text(encoding="ascii").strip()
        except (OSError, UnicodeError):
            return False
        return hmac.compare_digest(credential, expected)

    def status(self) -> dict[str, Any]:
        """Return persistent public progress, never filesystem or auth details."""
        state = self._read()
        return {
            key: state.get(key)
            for key in ("state", "tag", "commit", "message", "updated_at")
            if key in state
        }

    def availability(self) -> dict[str, Any]:
        """Only a newer Pi-capable release can be offered in the menu."""
        installed = source_version.RUNNING_SOURCE
        if not installed.get("commit") or installed.get("dirty") is not False:
            return {
                "available": False,
                "reason": "Installed source cannot be verified; use SSH deployment.",
            }
        try:
            release = release_source.discover_latest()
        except release_source.UnsupportedRelease:
            return {
                "available": False,
                "reason": "The latest release has no Pi update package.",
            }
        except release_source.ReleaseSourceError:
            return {
                "available": False,
                "reason": "Could not verify the latest GitHub release.",
            }
        if (
            Version(str(release.version)) <= Version(__version__)
            or release.commit == installed["commit"]
        ):
            return {
                "available": False,
                "reason": "This Pi already runs the latest Pi release.",
            }
        return {
            "available": True,
            "tag": release.tag,
            "commit": release.commit,
            "release_url": release.release_url,
        }

    def request_latest(self) -> dict[str, Any]:
        """Record a server-chosen release, then ask systemd to restart."""
        installed = source_version.RUNNING_SOURCE
        if not installed.get("commit") or installed.get("dirty") is not False:
            raise UpdateConflict(
                "Installed source cannot be verified; use SSH deployment"
            )
        release = release_source.discover_latest()
        if (
            Version(str(release.version)) <= Version(__version__)
            or release.commit == installed["commit"]
        ):
            raise UpdateConflict("There is no newer Pi release to install")
        try:
            with self._lock():
                if (self.state_dir / "manual-deploy.lock").exists():
                    raise UpdateConflict(
                        "An SSH deployment is in progress; try again after it completes"
                    )
                previous = self._read()
                if previous["state"] in _ACTIVE_STATES:
                    raise UpdateConflict("An update is already in progress")
                self._write(
                    {
                        "state": "requested",
                        "tag": release.tag,
                        "commit": release.commit,
                        "release_id": release.release_id,
                        "previous_commit": installed["commit"],
                        "message": f"Preparing {release.tag}; the controller will restart.",
                    }
                )
        except BlockingIOError as exc:
            raise UpdateConflict("Another deployment is in progress") from exc
        self.restart()
        return {"status": "accepted", "tag": release.tag, "commit": release.commit}

    def mark_healthy(self) -> bool:
        """A new process confirms it survived startup on the requested commit."""
        try:
            with self._lock():
                state = self._read()
                if state.get("state") != "awaiting_health":
                    return False
                if (
                    source_version.RUNNING_SOURCE.get("commit") != state.get("commit")
                    or source_version.RUNNING_SOURCE.get("dirty") is not False
                ):
                    return False
                self._write(
                    {
                        **state,
                        "state": "completed",
                        "message": f"Installed {state['tag']} successfully.",
                    }
                )
                return True
        except BlockingIOError:
            return False
