# Third-party software

Desk Controller depends on third-party Python packages. Their licenses remain
in effect and are not replaced by this project's MIT license.

The desktop release build creates `THIRD_PARTY_LICENSES.txt` from the exact
packages installed in the build environment and includes it beside the
application. Notable runtime components include:

- Eclipse Paho MQTT Python client — EPL-2.0 or BSD-3-Clause
- FastAPI and Pydantic — MIT
- Pillow — HPND
- psutil — BSD-3-Clause
- pycaw and comtypes — MIT
- pystray — LGPL-3.0
- PyInstaller — GPL-2.0-or-later with the PyInstaller bootloader exception
- PyObjC — MIT
- PyYAML — MIT
- Requests — Apache-2.0
- StreamDeck Python Library — MIT

The optional Acroname integration is not included in the desktop release
artifacts. Installing the `acroname` extra downloads Acroname's BrainStem
package separately; use and redistribution are governed by Acroname's own
license terms.

No NirSoft software is included in this repository or its release artifacts.
