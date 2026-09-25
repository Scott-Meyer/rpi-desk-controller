#!/usr/bin/env python3
"""Stable systemd ExecStartPre for installing and rolling back Pi releases.

Run with the source checkout root as the sole argument. This file is intentionally
stdlib-only: it must still work when the currently selected app cannot import.
The application requests an update by atomically writing config/.update/status.json;
its healthy replacement changes awaiting_health to completed after startup.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path


class UpdateError(Exception):
    """The previous service cannot safely be switched or restored."""


def _sync_dir(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_status(path: Path, status: dict) -> None:
    status = {**status, "updated_at": datetime.now(timezone.utc).isoformat()}
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".status-",
            suffix=".tmp",
            delete=False,
        ) as output:
            name = output.name
            os.chmod(name, 0o600)
            json.dump(status, output, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(name, path)
        _sync_dir(path.parent)
    finally:
        if name is not None and os.path.exists(name):
            os.unlink(name)


def _read_status(path: Path) -> dict | None:
    try:
        status = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise UpdateError("invalid update status JSON") from exc
    if (
        not isinstance(status, dict)
        or type(status.get("schema")) is not int
        or status["schema"] != 1
        or status.get("state")
        not in (
            "requested",
            "staging",
            "switching",
            "awaiting_health",
            "completed",
            "failed",
        )
    ):
        raise UpdateError("unknown update status schema or state")
    return status


def _interpreter(venv: Path) -> bool:
    python = venv / "bin" / "python"
    return python.is_file() and os.access(python, os.X_OK)


def _previous(root: Path, baseline: Path) -> dict:
    venv = root / "venv"
    if not _interpreter(venv):
        raise UpdateError("current venv/bin/python is unavailable")
    if venv.is_symlink():
        return {"kind": "symlink", "target": os.readlink(venv)}
    if not venv.is_dir():
        raise UpdateError("current venv is not a directory or symlink")
    if baseline.exists() or baseline.is_symlink():
        raise UpdateError("baseline venv already exists; refusing to overwrite it")
    return {"kind": "directory"}


def _replace_link(link: Path, target: str) -> None:
    """Replace a symlink (or a temporarily absent path) without a partial link."""
    temporary = link.parent / (".venv-switch-" + uuid.uuid4().hex)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, link)
        _sync_dir(link.parent)
    finally:
        if temporary.is_symlink():
            temporary.unlink()


def _rollback(root: Path, previous: object, baseline: Path) -> None:
    venv = root / "venv"
    if not isinstance(previous, dict):
        raise UpdateError("rollback target missing from update status")
    if previous.get("kind") == "directory" and set(previous) == {"kind"}:
        if baseline.is_dir() and not baseline.is_symlink():
            if not _interpreter(baseline):
                raise UpdateError("baseline interpreter is unavailable")
            if venv.exists() and not venv.is_symlink():
                raise UpdateError("cannot replace unexpected venv directory")
            _replace_link(venv, str(baseline))
        elif not baseline.exists() and not baseline.is_symlink():
            # Interrupted before the original directory was moved at all.
            if venv.is_symlink() or not _interpreter(venv):
                raise UpdateError("baseline missing and original venv unavailable")
        else:
            raise UpdateError("baseline venv has an unexpected type")
    elif previous.get("kind") == "symlink" and set(previous) == {"kind", "target"}:
        target = previous["target"]
        if not isinstance(target, str) or not target:
            raise UpdateError("invalid previous venv link")
        original = Path(target)
        if not original.is_absolute():
            original = root / original
        if not _interpreter(original):
            raise UpdateError("previous venv interpreter is unavailable")
        if venv.exists() and not venv.is_symlink():
            raise UpdateError("cannot replace unexpected venv directory")
        _replace_link(venv, target)
    else:
        raise UpdateError("invalid rollback target in update status")
    if not _interpreter(venv):
        raise UpdateError("restored venv interpreter is unavailable")


def _failed(status: dict, detail: str, summary: str) -> dict:
    return {
        **{key: value for key, value in status.items() if key != "previous_venv"},
        "state": "failed",
        "message": summary,
        "error": detail,
    }


def _identity(status: dict) -> tuple[str, str, int]:
    tag, commit, release_id = (
        status.get("tag"),
        status.get("commit"),
        status.get("release_id"),
    )
    if (
        not isinstance(tag, str)
        or len(tag) > 80
        or not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.]+)?", tag)
        or not isinstance(commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", commit)
        or type(release_id) is not int
        or release_id <= 0
    ):
        raise UpdateError("invalid requested release identity")
    return tag, commit, release_id


def run(root: Path) -> None:
    root = root.resolve(strict=True)
    state_path = root / "config" / ".update" / "status.json"
    status = _read_status(state_path)
    if status is None or status["state"] in ("completed", "failed"):
        return
    baseline = state_path.parent / "baseline" / "venv"
    if status["state"] == "staging":
        if not _interpreter(root / "venv"):
            raise UpdateError("prior interpreter missing after interrupted staging")
        _write_status(
            state_path,
            _failed(
                status,
                "installer was interrupted while preparing a release",
                "Update interrupted during preparation; previous installation remains active.",
            ),
        )
        return
    if status["state"] in ("switching", "awaiting_health"):
        # No fresh process has confirmed its health. Includes power loss in
        # the middle of the move/symlink operation.
        _rollback(root, status.get("previous_venv"), baseline)
        summary = (
            "New release exited before health confirmation; previous installation restored."
            if status["state"] == "awaiting_health"
            else "Update interrupted during activation; previous installation restored."
        )
        _write_status(
            state_path, _failed(status, "new release did not become healthy", summary)
        )
        return

    try:
        tag, commit, release_id = _identity(status)
        previous = _previous(root, baseline)
        destination = state_path.parent / "releases" / commit
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = {**status, "state": "staging", "message": f"Preparing {tag}."}
        _write_status(state_path, staging)
        subprocess.run(
            [
                str(root / "venv" / "bin" / "python"),
                str(root / "scripts" / "rpi_release_stage.py"),
                tag,
                commit,
                str(release_id),
                str(destination),
            ],
            cwd=root,
            check=True,
            timeout=1020,
        )
        if not _interpreter(destination / "venv"):
            raise UpdateError("staged release interpreter is unavailable")
    except (
        UpdateError,
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
    ) as exc:
        _write_status(
            state_path,
            _failed(
                status,
                f"release staging failed: {exc}",
                "Could not prepare release; previous installation remains active.",
            ),
        )
        return

    switching = {
        **status,
        "state": "switching",
        "previous_venv": previous,
        "message": f"Switching to {tag}.",
    }
    _write_status(state_path, switching)
    try:
        if previous["kind"] == "directory":
            baseline.parent.mkdir(parents=True, exist_ok=True)
            os.replace(root / "venv", baseline)
            _sync_dir(root)
            _sync_dir(baseline.parent)
        _replace_link(root / "venv", str(destination / "venv"))
        if not _interpreter(root / "venv"):
            raise UpdateError("selected release interpreter is unavailable")
        _write_status(
            state_path,
            {
                **switching,
                "state": "awaiting_health",
                "message": f"Starting {tag}; verifying health.",
            },
        )
    except (OSError, UpdateError) as exc:
        # Leave switching in place if rollback fails: on the next start the
        # bootstrap must retry rather than running an unconfirmed interpreter.
        _rollback(root, previous, baseline)
        _write_status(
            state_path,
            _failed(
                switching,
                f"release switch failed: {exc}",
                "Could not activate release; previous installation restored.",
            ),
        )


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: rpi_update_bootstrap.py PROJECT_ROOT", file=sys.stderr)
        return 2
    try:
        run(Path(sys.argv[1]))
    except (OSError, UpdateError) as exc:
        print(f"Pi updater bootstrap: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
