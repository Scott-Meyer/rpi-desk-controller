#!/usr/bin/env bash

# Build script for macOS Desktop Agent
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "The macOS build must run on macOS." >&2
    exit 2
fi

PYTHON_BIN="${PYTHON_BIN:-python3}"
SIGNING_IDENTITY="${MACOS_SIGNING_IDENTITY:--}"

echo "=== Installing macOS build dependencies ==="
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -e ".[desktop,build]"

mkdir -p build
"$PYTHON_BIN" -m piplicenses \
    --with-license-file \
    --with-notice-file \
    --no-license-path \
    --format=plain-vertical \
    --ignore-packages desk-controller \
    --output-file build/THIRD_PARTY_LICENSES.txt

echo "=== Building standalone macOS application bundle ==="
"$PYTHON_BIN" -m PyInstaller \
    --clean \
    --noconfirm \
    --windowed \
    --name="DeskAgent-macOS" \
    --specpath="build" \
    --paths="src" \
    --hidden-import="pystray._darwin" \
    --exclude-module="brainstem" \
    --collect-data="desk_controller.desktop_agent" \
    --add-data="$SCRIPT_DIR/build/THIRD_PARTY_LICENSES.txt:." \
    --copy-metadata="desk-controller" \
    src/desk_controller/desktop_agent/main_tray.py

if [[ ! -d "dist/DeskAgent-macOS.app" ]]; then
    echo "PyInstaller did not create dist/DeskAgent-macOS.app." >&2
    exit 1
fi

codesign --force --deep --sign "$SIGNING_IDENTITY" "dist/DeskAgent-macOS.app"
hdiutil create \
    -volname "DeskAgent" \
    -srcfolder "dist/DeskAgent-macOS.app" \
    -ov \
    -format UDZO \
    "dist/DeskAgent-macOS.dmg"

test -s "dist/DeskAgent-macOS.dmg"
echo "=== macOS Build Complete! Disk image saved to dist/DeskAgent-macOS.dmg ==="
