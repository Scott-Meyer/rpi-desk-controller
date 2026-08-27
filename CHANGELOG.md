# Changelog

All notable changes to this project will be documented here.

## 1.1.0 - 2026-08-26

- Added hard-stop handling on MQTT authentication and authorization failures to prevent reconnect flapping loops.
- Added pre-save MQTT credential verification in native settings dialogs and web configuration editor.
- Added live "Test Connection" button and real-time connection status feedback in settings popup and web UI.
- Added web-based MQTT settings editor to view, test, and update broker credentials directly in the browser.
- Improved desktop tray state indication and notifications for authentication errors.

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
