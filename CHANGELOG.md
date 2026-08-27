# Changelog

All notable changes to this project will be documented here.

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
