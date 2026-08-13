# Security policy

## Supported versions

Security fixes are applied to the latest release and the default branch.

## Deployment boundary

Desk Controller is intended for a trusted private network:

- The HTTP API and Pi configuration UI bind to loopback by default. Use the
  documented SSH tunnel rather than exposing port 8080.
- Fresh Pi-hosted MQTT installations require generated credentials.
- Never forward MQTT port 1883 or HTTP port 8080 through a public router or
  firewall.
- Keep `config/config.yaml` private. It contains MQTT and optional Home
  Assistant credentials and is written with owner-only permissions.
- Desktop updates are manual. Review the GitHub release, checksum, and source
  before installing an unsigned artifact.

Changing `server.host` away from loopback exposes an unauthenticated operational
API. If remote HTTP access is required, put the controller behind an
authenticated TLS reverse proxy and restrict access with a firewall or VPN.

## Reporting a vulnerability

Do not open a public issue for a suspected vulnerability or exposed secret.
Use GitHub's private vulnerability reporting flow:

https://github.com/Scott-Meyer/rpi-desk-controller/security/advisories/new

Include the affected version, deployment topology, reproduction steps, and
impact. Remove real credentials, hostnames, device serial numbers, and private
IP addresses from the report.
