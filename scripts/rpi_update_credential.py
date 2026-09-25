#!/usr/bin/env python3
"""Provision the local-only administrator credential for Pi code updates.

Run from trusted SSH setup/deployment; never print the credential to logs.
"""

import os
import secrets
import sys
from pathlib import Path


def provision(root: Path) -> None:
    directory = root / "config" / ".update"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "admin-token"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Unsafe existing update credential path")
        os.chmod(path, 0o600)
        return
    with os.fdopen(fd, "w", encoding="ascii") as output:
        output.write(secrets.token_urlsafe(32) + "\n")
        output.flush()
        os.fsync(output.fileno())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: rpi_update_credential.py PROJECT_ROOT")
    provision(Path(sys.argv[1]))
    print("Pi update administrator credential is provisioned (value not displayed).")
