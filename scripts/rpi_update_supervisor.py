#!/usr/bin/env python3
"""Stable systemd main process for a candidate Pi release.

Unlike a watchdog inside the candidate, this still runs if the candidate hangs
while importing or initializing its hardware. A timed-out candidate exits so
the next ExecStartPre can restore the previous environment.
"""

import json
import subprocess
import sys
import time
from pathlib import Path

HEALTH_TIMEOUT_SECONDS = 90


def _status(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "idle"
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise ValueError("invalid Pi update status")
    state = data.get("state")
    if state not in {
        "requested",
        "staging",
        "switching",
        "awaiting_health",
        "completed",
        "failed",
    }:
        raise ValueError("unknown Pi update state")
    return state


def run(root: Path, health_timeout: float = HEALTH_TIMEOUT_SECONDS) -> int:
    root = root.resolve(strict=True)
    state_path = root / "config" / ".update" / "status.json"
    pending = _status(state_path) == "awaiting_health"
    command = [
        str(root / "venv" / "bin" / "python"),
        "-m",
        "desk_controller.pi_controller.main",
    ]
    child = subprocess.Popen(command, cwd=root)
    started = time.monotonic()
    try:
        while True:
            result = child.poll()
            if result is not None:
                return result
            if pending:
                state = _status(state_path)
                if state == "completed":
                    pending = False
                elif state != "awaiting_health":
                    raise RuntimeError("candidate update status changed unexpectedly")
                elif time.monotonic() - started >= health_timeout:
                    raise TimeoutError(
                        "new controller did not confirm health before deadline"
                    )
            time.sleep(0.25)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        print(
            f"Pi update candidate failed: {exc}; systemd will restore previous version",
            file=sys.stderr,
            flush=True,
        )
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return 1


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: rpi_update_supervisor.py PROJECT_ROOT", file=sys.stderr)
        return 2
    try:
        return run(Path(sys.argv[1]))
    except (OSError, ValueError) as exc:
        print(f"Pi update supervisor: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
