#!/usr/bin/env bash

# SSH deployment script to sync local repo to Raspberry Pi and restart desk controller service
set -euo pipefail

if [[ "$#" -lt 1 || "$#" -gt 2 ]]; then
    echo "Usage: $0 <user@rpi-host> [remote-directory]" >&2
    exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RPI_TARGET="$1"
REMOTE_DIR="${2:-rpi-desk-controller}"

if [[ "$REMOTE_DIR" == *" "* || "$REMOTE_DIR" == *"'"* ]]; then
    echo "Remote paths containing spaces or single quotes are not supported: $REMOTE_DIR" >&2
    exit 2
fi

# The Pi receives source files, not .git. Record the exact checkout used for
# this rsync; if local files differ from HEAD, never claim a clean revision.
SOURCE_COMMIT="$(git -C "$PROJECT_DIR" rev-parse --verify HEAD)"
SOURCE_DIRTY=0
if [[ -n "$(git -C "$PROJECT_DIR" status --porcelain --untracked-files=normal -- README.md LICENSE THIRD_PARTY_NOTICES.md config/config.example.yaml src scripts systemd pyproject.toml requirements.txt)" ]]; then
    SOURCE_DIRTY=1
fi

echo "=== Creating remote directory on $RPI_TARGET ==="
# REMOTE_DIR is deliberately expanded locally and single-quoted for the remote shell.
# shellcheck disable=SC2029
ssh "$RPI_TARGET" "mkdir -p '$REMOTE_DIR'"

echo "=== Syncing application sources to $RPI_TARGET:$REMOTE_DIR ==="
(
    cd "$PROJECT_DIR"
    rsync -az --relative \
        ./README.md \
        ./LICENSE \
        ./THIRD_PARTY_NOTICES.md \
        ./pyproject.toml \
        ./requirements.txt \
        ./config/config.example.yaml \
        "$RPI_TARGET:$REMOTE_DIR/"
    # These directories are owned by the release. Delete stale files inside
    # them so removed entry points cannot continue to run after an upgrade.
    rsync -az --delete ./scripts/ "$RPI_TARGET:$REMOTE_DIR/scripts/"
    rsync -az --delete ./src/ "$RPI_TARGET:$REMOTE_DIR/src/"
    rsync -az --delete ./systemd/ "$RPI_TARGET:$REMOTE_DIR/systemd/"
)

echo "=== Installing the package and restarting the service ==="
# REMOTE_DIR is deliberately expanded locally and single-quoted for the remote shell.
# shellcheck disable=SC2029
ssh "$RPI_TARGET" "bash -s -- '$REMOTE_DIR' '$SOURCE_COMMIT' '$SOURCE_DIRTY'" <<'REMOTE_SCRIPT'
set -euo pipefail

remote_dir="$1"
source_commit="$2"
source_dirty="$3"
cd "$remote_dir"
remote_dir="$(pwd)"

# Atomically stamp the files just transferred. The import-time reader keeps
# the old identity until the new process starts after this write.
python3 - "$source_commit" "$source_dirty" <<'STAMP_SCRIPT'
import json
import os
import sys
from pathlib import Path

commit, dirty = sys.argv[1:]
path = Path("src/desk_controller/_source_version.json")
tmp = path.with_suffix(".json.tmp")
tmp.write_text(json.dumps({"commit": commit, "dirty": dirty == "1"}) + "\n")
os.replace(tmp, path)
STAMP_SCRIPT

if [[ ! -x venv/bin/python ]]; then
    python3 -m venv venv
fi

venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -e ".[pi,acroname]"

if [[ ! -f config/config.yaml ]]; then
    install -m 0600 config/config.example.yaml config/config.yaml
    echo "Created config/config.yaml. Add credentials, then run scripts/setup_rpi.sh on the Pi."
    exit 0
fi

chmod 0600 config/config.yaml

if systemctl cat desk-controller.service >/dev/null 2>&1; then
    service_user="$(id -un)"
    service_group="$(id -gn)"
    sed \
        -e "s|@SERVICE_USER@|$service_user|g" \
        -e "s|@SERVICE_GROUP@|$service_group|g" \
        -e "s|@PROJECT_DIR@|$remote_dir|g" \
        systemd/desk-controller.service |
        sudo tee /etc/systemd/system/desk-controller.service >/dev/null
    sudo chmod 0644 /etc/systemd/system/desk-controller.service
    sudo systemctl daemon-reload
    sudo systemctl enable desk-controller.service >/dev/null
    sudo systemctl restart desk-controller.service
    if ! sudo systemctl is-active --quiet desk-controller.service; then
        sudo systemctl status --no-pager desk-controller.service
        exit 1
    fi
else
    echo "desk-controller.service is not installed; run scripts/setup_rpi.sh on the Pi once."
fi
REMOTE_SCRIPT

echo "=== Deployment successful! ==="
