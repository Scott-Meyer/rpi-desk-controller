# Changelog

All notable changes to this project will be documented here.

## Unreleased

- Scene keys show the same dotted pending border as other keys after a press,
  until Home Assistant reports that scene (or, for a press that turns the
  scene off, until its lights report off); their label is unchanged.
- Scene keys show what Home Assistant reports right now, so the active key
  survives restarts and reflects changes made anywhere. Home Assistant-defined
  scenes (the light scenes) are matched against their stored states:
  brightness, colour, and effect. Integration-owned scenes whose targets Home
  Assistant does not hold (PowerView shade scenes) show the most recently
  activated scene.
- With a Pi-owned Acroname hub, the selected computer is always the host the
  hub reports. The Stream Deck, MQTT state, audio, and workstation keys
  follow it, including when the monitor step fails or USB is moved by
  another tool, and the monitor is then sent to that computer.
- KVM requests are no longer dropped: the latest request wins and USB, then
  the monitor, are retried with backoff until they arrive. Pressing the key
  for the current computer re-sends the monitor input. A failed hub reading
  is reported as unknown instead of the last commanded host.
- Saving the Pi configuration page keeps monitor settings the page does not
  show, such as `use_alt_addressing`. Previously a save dropped it, so LG
  alt-input monitors acknowledged KVM switches without changing input.

## 1.2.1 - 2026-09-29

- The Pi reconnects a Stream Deck after it is unplugged and reattached, restoring
  brightness, key artwork, and press handling without a service restart.
  Callbacks from an already detached device are ignored.
- Added a manual, administrator-authorized Pi update button for verified GitHub
  releases, with installation outside the running service and startup rollback.

## 1.2.0 - 2026-09-14

- Added Pi-native LG alt-input DDC/CI switching, using the manufacturer's
  DDC2AB I2C side-channel address so KVM transactions actually move the
  monitor instead of silently no-oping.
- Added real hardware detection of the active USB host at startup so the
  Pi no longer trusts a possibly-stale configured default and misreports
  which PC currently has keyboard/mouse control.
- Made the physical Stream Deck KVM toggle button non-blocking: presses now
  give instant pending feedback and no longer queue up or feel unresponsive
  while a switch is in flight.
- Added BetterDisplay alt-addressing (`ddcAlt`/`inputSelectAlt`) support to
  the macOS desktop agent's monitor controller.

## 1.1.0 - 2026-08-26

- Added customizable Desk Controller Name and Unique Device ID in Web UI and Home Assistant MQTT Auto-Discovery for seamless multi-desk / multi-Pi setups.
- Added direct private LAN hosting for the Pi Web Configuration UI on port 8080 without requiring SSH tunnels.
- Added in-browser GitHub Release Update Checker and one-click system update & restart buttons on the Raspberry Pi Web UI.
- Added direct cross-link buttons between PC Desktop Agent Web UI and the connected Pi Desk Controller Web UI.
- Added dynamic wildcard Stream Deck layout discovery and dynamic MQTT Client ID handling.
- Added hard-stop handling on MQTT authentication and authorization failures to prevent reconnect flapping loops.
- Added pre-save MQTT credential verification and live "Test Connection" feedback in desktop settings and web UI.

## 1.0.0 - 2026-08-20

- Added coordinated Raspberry Pi, monitor, and USB-switch KVM transactions.
- Added Windows and macOS desktop agents with native audio control.
- Added optional Stream Deck, Home Assistant, Acroname, and UGREEN integrations.
- Added workstation-owned buttons, global KVM shortcuts, and USB sentinel
  verification.
- Added bounded Pi-local retries and dotted yellow pending feedback for
  laptop-bound Stream Deck actions.
- Added authenticated fresh-install MQTT configuration and loopback-only HTTP
  defaults.
- Added CI, release packaging, dependency auditing, and third-party license
  inventories.
