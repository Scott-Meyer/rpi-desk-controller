# Raspberry Pi 4 Smart Desk & KVM Controller

A network KVM and desk controller built around a Raspberry Pi, a supported USB
host switch, and Windows or macOS workstation agents. An Elgato
Stream Deck, DDC/CI monitor switching, and Home Assistant can be added without
being required for the core two-computer KVM.

> **Beta:** This project controls physical USB and monitor routing. Test with
> simulation first, keep manual access to the hardware, and do not expose its
> MQTT broker or HTTP API to the public Internet.

## Requirements

- Python 3.10 or newer.
- Raspberry Pi OS or another Debian-based Pi distribution for the controller.
- Windows 10/11 or a recent macOS release for the desktop agent.
- A supported USB switch. DDC/CI, Stream Deck, Home Assistant, and desktop-side
  hardware control are optional.

## Components

- **Pi controller:** coordinates KVM transactions, workstation state, optional
  hardware integrations, and the local REST-to-MQTT bridge.
- **Desktop agent:** publishes workstation telemetry and switches the active
  audio output from MQTT or its system-tray menu.
- **MQTT:** carries commands and retained state between the Pi and
  workstations. It can run on the Pi or on an existing broker.

The minimum useful installation is a Pi, two workstation agents, and a USB
host switch. The agents' global shortcut can request the same coordinated KVM
toggle as a physical button, so a Stream Deck is optional. Home Assistant is
also optional; enabling it adds scene and service actions plus MQTT
discovery without changing the core control plane.

The REST API is a facade over MQTT rather than a second control plane:

- `GET /api/v1/telemetry` returns the latest validated
  `desk/<device>/telemetry` messages.
- `POST /api/v1/audio/select` publishes the requested device to
  `desk/<device>/audio/set` and returns `503` when MQTT is unavailable.
- `GET /api/v1/usb-hub` returns live hub identity and per-port state.
- `PUT /api/v1/usb-hub` switches the synchronized monitor/USB host.
- `PUT /api/v1/usb-hub/ports/<0-7>` controls one port or runs a reset action.

Every application publishes retained network identity at
`desk/<device-id>/status`. The payload contains `device_id`, `hostname`,
`os_type`, `lan_ip`, and `app_version`; desktop telemetry also includes
`lan_ip`. The status is refreshed after every MQTT reconnect so DHCP changes
are reflected automatically.

Desktop agents separately publish retained liveness at
`desk/<device-id>/availability`. An online birth message is sent only after
fresh retained application state, and an MQTT Last Will changes availability
to `offline` after an unexpected disconnect. Paho performs fast reconnects
with a one-to-thirty-second backoff. If one client generation remains offline
for ninety seconds, an independent supervisor replaces its complete socket and
resolver state, reapplies credentials, subscriptions, and Last Will, and lets
the normal online birth sequence run again. Connection health reports the
offline duration, last disconnect reason, and recovery count without exposing
credentials.

While MQTT is offline, the desktop agent suspends its CPU, memory, audio, media,
process, and USB-sentinel polling. Only the backoff-driven MQTT connection loop
continues, and normal polling resumes immediately after reconnecting. The tray
icon is green while connected and amber with a slash while offline; its tooltip
and first menu item show the same state. The local button editor refreshes its
connection status automatically and clearly distinguishes a local-only save
that is waiting to publish after reconnection.

The Pi publishes its physical controls and USB hub under
`desk/rpi_desk_controller`:

- `streamdeck/action` is a simple `key_0` through `key_14` press event.
- `streamdeck/event` is a richer JSON press event containing the physical
  row, column, label, action, and state group.
- `streamdeck/layout` and `streamdeck/state` are retained so consumers can
  discover the complete 5-by-3 layout and its active groups.
- Every key without a Pi action automatically belongs to the currently
  selected workstation. The Pi caches every workstation profile, leaves
  unassigned or offline keys blank, and illuminates a key while its
  workstation-owned state is active.
- `usb_hub/config` and `usb_hub/state` are retained and describe the selected
  controller and its capabilities. With Acroname they also contain all eight
  editable port names plus live attached-device descriptors, enable state,
  USB 2/3 line state, voltage, current, power, mode, speed, and detailed
  errors.
- For Acroname, `usb_hub/port/<0-7>/set` accepts `ON`, `OFF`, or JSON
  containing checked
  `enabled`, `power_enabled`, `data_enabled`, `usb2_data_enabled`,
  `usb3_data_enabled`, `current_limit_ma`, and `port_mode` fields. The
  `reset_data`, `reset_power`, and `clear_errors` actions are also supported.
  Per-port JSON state and simple `ON`/`OFF` state are retained beside it.

When enabled, Home Assistant MQTT discovery registers every Stream Deck key as
a device trigger. Each controllable Acroname USB port receives switches for
the whole port, power, USB 2, and USB 3; electrical and device sensors;
attachment state; current-limit and charging-mode controls; and
reset/error-clear buttons. Discovery is republished when Home Assistant sends
its MQTT birth message.

Set `hardware.simulate: true` only for development without the USB hub or
`ddcutil`. Missing hardware fails closed by default and marks the Home
Assistant KVM entity unavailable.

On startup, the Pi reads the monitor's current DDC/CI input and routes the USB
switch to the matching workstation without changing the display input. If the
monitor is off or its input cannot be read, `usb_switch.default_channel` is
used as the fallback host.

## USB host-switch backends

The Pi-side USB hardware is selected independently from KVM ownership:

```yaml
usb_switch:
  driver: acroname
  default_channel: 0
```

`acroname` is the default and retains full upstream selection, eight-port
control, device discovery, and electrical telemetry. Existing configurations
that contain only the legacy `acroname` section continue to work.

The UGREEN CM691 / P/N 25164 USB-C 2-in/4-out switch has no software control
or host-state API. Its included desktop controller is a momentary button, so
the `ugreen_cm691_gpio` backend drives an isolated relay or optocoupler wired
in parallel with that button:

```yaml
usb_switch:
  driver: ugreen_cm691_gpio
  default_channel: 0
  gpio_pin: 17          # BCM numbering
  gpio_active_high: false
  gpio_pulse_ms: 200
  observation_enabled: true
  sentinel_vendor_id: 0x046d
  sentinel_product_id: 0x0825
  sentinel_serial_number: CAM123  # optional
  observation_poll_interval: 2
  observation_timeout: 8
```

Do not connect the UGREEN control-port conductors directly to a Raspberry Pi
GPIO. Use a galvanically isolated dry-contact relay or optocoupler and verify
the desktop-controller button contacts with a multimeter before wiring them.

For deterministic targeting, configure one always-connected device on the
switch as the USB sentinel. The Pi publishes that identity to both workstation
agents. Each agent reports whether the sentinel is locally present, and the Pi
only commits a UGREEN switch after the target workstation observes it. Missing,
stale, failed, or conflicting observations produce an `unknown` state instead
of a guessed success. The configuration page reports `synced`, `diverged`, or
`unknown` and provides a force-sync action.

Without sentinel observation, `default_channel` must match the illuminated
host when the service starts and manual button presses cannot be detected.
Per-port power controls and electrical telemetry remain unavailable for this
backend.

## Distributed KVM hardware and global shortcut

The Pi remains the KVM transaction coordinator even when one or both hardware
control connections are attached to a computer. Configure monitor and USB
ownership independently in the Pi UI or YAML:

```yaml
kvm:
  monitor_controller: active_workstation
  usb_controller: pi
  remote_timeout: 10
```

Each controller can be `pi`, `active_workstation`, `pc1`, or `pc2`.
`active_workstation` delegates the step to the agent for the currently selected
host; `pc1` and `pc2` always delegate to that fixed workstation. The Pi sends
correlated, allowlisted MQTT commands and retains the USB-first, monitor-second
transaction. A monitor failure rolls USB back before the KVM state is marked
unavailable.

Enable only the hardware physically connected to a desktop agent:

```yaml
desktop_kvm:
  hotkey:
    enabled: true
    shortcut: "ctrl+option+command+k"
  monitor:
    enabled: true
    backend: betterdisplay
    display_name: "Odyssey G75F"
    inputs:
      pc1: "0x0f"
      pc2: "0x11"
  usb:
    enabled: false
    serial_number:
```

macOS monitor control uses BetterDisplay's DDC CLI; Windows uses the native
physical-monitor VCP API. A desktop-controlled Acroname hub uses the same
BrainStem driver as the Pi. Disabled local hardware commands fail closed.

The native global shortcut asks the Pi for a coordinated toggle, so it behaves
the same whether all hardware is on the Pi, all hardware is delegated, or
ownership is mixed. It does not require macOS Accessibility permission.
Duplicate QoS 1 shortcut requests and delegated commands are processed only
once.

## Raspberry Pi installation

The setup script installs system packages (including Mosquitto), creates
`venv`, installs the Python package with its Pi and Acroname extras, configures
udev, and installs the systemd service for the current user.

```bash
git clone https://github.com/Scott-Meyer/rpi-desk-controller.git
cd rpi-desk-controller
./scripts/setup_rpi.sh
```

On the first run, the script creates `config/config.yaml` with permissions
`0600`, generates a random MQTT password, configures an authenticated
Pi-hosted broker, and starts the controller. Fresh installations use that
local broker and leave Home Assistant disabled. Copy the generated MQTT
username and password from `config/config.yaml` over SSH when configuring each
desktop agent; the web UI intentionally never returns saved secrets.
Values explicitly set in existing configurations are preserved, while missing
keys adopt the new secure defaults. Existing configuration files are never
overwritten. The controller HTTP server is loopback-only by default. Open its
configuration UI through an SSH tunnel:

```bash
ssh -L 8080:localhost:8080 user@rpi-host
```

Then browse to `http://localhost:8080/config`.

The page configures connections, hardware routing, USB hardware, workstation
IDs, and the full physical Stream Deck layout. With Acroname, its live
dashboard also shows attached-device descriptors and electrical telemetry and
controls power, USB 2/3 data lines, charging mode, current limit, device
re-enumeration, power cycling, and error clearing. Each Stream Deck key
can define its label, icon, color, action, target, mutually-exclusive state
group, and optional MQTT feedback. Stored secrets are never sent back to the
browser. **Save & restart** gracefully reloads the service through systemd.

In the Connections tab, select **Hosted by this Pi** for a standalone
installation or **External broker** to keep using Home Assistant's or another
MQTT service. Workstation agents must use the Pi's LAN address and port 1883 in
standalone mode. The setup script's broker listener is intended for a trusted
private LAN, requires the generated credentials, and must not be exposed
through a router or public firewall.

Log out and back in once after setup so the `gpio`, `i2c`, and `plugdev` group
changes take effect.

## Desktop agent installation from source

Create a virtual environment and install the desktop dependency set:

```bash
python -m venv venv
source venv/bin/activate
python -m pip install -e ".[desktop]"
desk-agent
```

On Windows, activate the environment with `venv\Scripts\activate`. The macOS
agent uses the system CoreAudio framework directly and needs no separate audio
utility; Windows uses the native Core Audio APIs through `pycaw`. On first
launch, the desktop app opens an MQTT setup dialog. Use
**MQTT Settings…** in the tray menu to change the connection later; changes
are saved with owner-only permissions and applied without restarting the app.

Desktop-side Acroname control is intentionally excluded from the downloadable
desktop artifacts. To enable it in a source installation, accept Acroname's
license terms and install `.[desktop,acroname]`.

The agent finds configuration in this order:

1. The path in `DESK_CONTROLLER_CONFIG`.
2. `config/config.yaml` under the current working directory.
3. `config/config.yaml` or `config.yaml` beside a packaged executable.
4. The user configuration location:
   - Windows: `%APPDATA%\DeskController\config.yaml`
   - macOS: `~/Library/Application Support/DeskController/config.yaml`
   - Linux: `${XDG_CONFIG_HOME:-~/.config}/desk-controller/config.yaml`

The desktop settings dialog writes its per-user configuration automatically.
For manual Pi setup, copy
[config/config.example.yaml](config/config.example.yaml) to the selected
location. `config/config.yaml` is ignored by Git and must never be committed.

The desktop agent also hosts a button editor on loopback at
`http://127.0.0.1:8765`. Open **Configure Stream Deck Buttons…** from the tray
menu. The editor mirrors the retained 5-by-3 layout published by the Pi.
Pi-owned keys are visible but locked, and every empty key can be assigned
locally. Saving preserves connection credentials, applies the new registry
without restarting, and immediately republishes the retained manifest and
active state.

## Selected-workstation buttons

The Pi owns every key configured in its editor. Any empty key is automatically
available to the selected computer, with its human-facing physical number as
the slot ID. For example, physical key 15 is workstation slot `"15"`.
Legacy explicit `workstation_slot` entries remain supported and migrate when
the Pi layout is next saved.

Each computer independently assigns its available keys in the desktop editor
or configuration:

```yaml
workstation_slots:
  "14":
    label: "PLAY/PAUSE"
    icon: play
    accent_color: [80, 180, 255]
    action_type: media_play_pause
  "15":
    label: "MUTE"
    icon: mute
    accent_color: [255, 80, 80]
    action_type: audio_mute
```

Pressing the key sends only the allowlisted `slot_id` and `activate` action to
the selected workstation. The local desktop agent decides whether that means
muting audio or activating an application. Executable paths and local action
details are never advertised to the Pi.

Application buttons use `action_type: app`, plus `process_names` for their
active indicator and a local `launch_target`.

The workstation action catalog also supports:

- `open` to open a local file or URL with the computer's default application.
- `hotkey` to send a validated keyboard shortcut such as
  `command+shift+k`.
- `audio_mute` and `media_play_pause` for native system controls.

Keyboard actions require the operating system's keyboard-control permission.
Local action details remain on that computer and are never accepted from MQTT.

On macOS, `action_type: media_play_pause` uses the system-wide Now Playing
session. Its retained presentation updates with the current track title and
play/pause icon, while its active state follows playback.

The retained workstation contract is:

- `desk/<device-id>/deck/manifest`: labels, icons, and colors.
- `desk/<device-id>/deck/state`: complete per-slot active state.
- `desk/<device-id>/deck/command`: selected-host activation commands.
- `desk/<device-id>/availability`: desktop-agent online/offline state.

On macOS, `launch_target` is an application name accepted by `open -a`. On
Windows, it is a path or registered target accepted by `os.startfile`.

## General button actions

Pi-owned keys can switch the KVM, show the Pi's current local time or date,
select the active workstation's audio output, publish arbitrary MQTT, or call
Home Assistant. Date and time buttons refresh automatically once per minute.
Laptop-bound audio and workstation-slot actions remain queued on the Pi for
five minutes by default. A dotted yellow key border means delivery is still
pending; it disappears when the desktop confirms the action or when the retry
window expires. Configure the window with `streamdeck.pending_request_timeout`
and the cadence with `streamdeck.pending_retry_interval`.
MQTT command and feedback topics provide the generic integration path for
networked devices that do not have a dedicated provider.

Home Assistant is not limited to scenes. A `ha_service` action calls any
validated `domain.service`, with an optional entity and JSON service data:

```yaml
streamdeck:
  buttons:
    4:
      enabled: true
      label: "FAN\n60%"
      icon: none
      action_type: ha_service
      service: fan.set_percentage
      target: fan.office
      service_data:
        percentage: 60
```

Actions and visual feedback are independent: any Pi action may also configure
an exact MQTT feedback topic and active payload.

## Deploying updates to the Pi

Run the deployment script from any directory. It synchronizes application
sources without copying or deleting `config/config.yaml` or `venv`, reinstalls
the Pi dependency set, and restarts an installed service.

```bash
./scripts/deploy.sh user@rpi-host
./scripts/deploy.sh user@rpi-host /custom/remote/path
```

Run `scripts/setup_rpi.sh` on the Pi once before using remote deployment.

## Building desktop releases

Both build scripts install the same `desktop` and `build` extras declared in
`pyproject.toml`.

```bash
# Windows
build_win.bat

# macOS
./build_mac.sh
```

Outputs:

- `dist/DeskAgent-Windows.exe`
- `dist/DeskAgent-macOS.dmg`, containing an ad-hoc-signed application bundle

Each application includes a license inventory generated from the exact build
environment. BrainStem and NirSoft software are not included. The current
Windows artifact is unsigned, and the macOS artifact is ad-hoc signed but not
notarized; review the release checksum and source before running them. The tray
update check opens the canonical GitHub release page and never downloads or
executes an update automatically.

## Publishing a release

`src/desk_controller/__init__.py` is the single source of the package and
updater version. Before tagging, run the release workflow manually from
GitHub Actions. A manual run builds both desktop artifacts without creating a
release, so the exact outputs can be smoke-tested on Windows and macOS.

After completing the [release checklist](RELEASING.md), push a matching tag:

```bash
git tag v1.0.0
git push origin v1.0.0
```

The release workflow rejects mismatched tags, builds both platforms through
the local build scripts, publishes the `.exe` and `.dmg`, and attaches SHA-256
checksums.

## Security, contributing, and license

See [SECURITY.md](SECURITY.md) before changing network exposure or reporting a
vulnerability. Contributions are described in
[CONTRIBUTING.md](CONTRIBUTING.md).

The project source is licensed under the [MIT License](LICENSE). Dependency
licenses and optional proprietary integrations are summarized in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
