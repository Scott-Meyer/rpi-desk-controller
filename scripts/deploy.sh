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

# Reserve the Pi for this SSH deployment before touching release-owned files.
# A stale marker after a broken SSH session must be removed deliberately over
# SSH; the LAN updater will fail closed rather than race a partial deployment.
DEPLOY_ID="$(python3 -c 'import uuid; print(uuid.uuid4())')"
ssh "$RPI_TARGET" "bash -s -- '$REMOTE_DIR' '$DEPLOY_ID'" <<'DEPLOY_GUARD'
set -euo pipefail
cd "$1"
mkdir -p config/.update
python3 - "$2" <<'PY'
import fcntl
import json
import os
import sys
from pathlib import Path

identifier = sys.argv[1]
directory = Path('config/.update')
with (directory / 'install.lock').open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = directory / 'status.json'
    if state.exists() and json.loads(state.read_text()).get('state') in {
        'requested', 'staging', 'switching', 'awaiting_health'
    }:
        raise SystemExit('Pi web update is active; wait for it before SSH deployment')
    marker = directory / 'manual-deploy.lock'
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise SystemExit('Another or interrupted SSH deployment holds the Pi; inspect the marker over SSH')
    with os.fdopen(fd, 'w') as output:
        output.write(identifier + '\n')
        output.flush()
        os.fsync(output.fileno())
PY
DEPLOY_GUARD
trap 'echo "SSH deployment did not complete. Inspect config/.update/manual-deploy.lock on the Pi before clearing it; web updates remain blocked." >&2' ERR

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
ssh "$RPI_TARGET" "bash -s -- '$REMOTE_DIR' '$SOURCE_COMMIT' '$SOURCE_DIRTY' '$DEPLOY_ID'" <<'REMOTE_SCRIPT'
set -euo pipefail

remote_dir="$1"
source_commit="$2"
source_dirty="$3"
deploy_id="$4"
cd "$remote_dir"
remote_dir="$(pwd)"
python3 scripts/rpi_update_credential.py "$remote_dir"
clear_guard() {
    python3 - "$deploy_id" <<'CLEAR_GUARD'
import sys
from pathlib import Path
marker = Path('config/.update/manual-deploy.lock')
if marker.read_text().strip() != sys.argv[1]:
    raise SystemExit('Deployment marker changed; refusing to remove another deployment lock')
marker.unlink()
CLEAR_GUARD
}

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

if [[ -L venv ]]; then
    # Never mutate a verified release slot. Prepare a separate SSH-owned venv,
    # then select it only after its dependencies have installed successfully.
    manual_venv="config/.update/manual/$(date +%Y%m%dT%H%M%S)-$$/venv"
    python3 -m venv "$manual_venv"
    "$manual_venv/bin/python" -m pip install --upgrade pip
    "$manual_venv/bin/python" -m pip install -e ".[pi,acroname]"
    ln -s "$remote_dir/$manual_venv" ".venv-deploy-$$"
    mv -Tf ".venv-deploy-$$" venv
else
    if [[ ! -x venv/bin/python ]]; then
        python3 -m venv venv
    fi
    venv/bin/python -m pip install --upgrade pip
    venv/bin/python -m pip install -e ".[pi,acroname]"
fi

if [[ ! -f config/config.yaml ]]; then
    install -m 0600 config/config.example.yaml config/config.yaml
    clear_guard
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
clear_guard
REMOTE_SCRIPT
trap - ERR

echo "=== Deployment successful! ==="
