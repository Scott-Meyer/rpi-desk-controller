# Contributing

Bug reports and focused pull requests are welcome.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[pi,desktop,dev]"
python -m unittest discover -s tests
ruff check .
ruff format --check .
bandit -q -r src -ll
```

Platform-specific hardware is optional during development. Use
`hardware.simulate: true` only in a private development configuration.

## Pull requests

- Add tests for behavior changes and failure paths.
- Keep hardware operations fail-closed.
- Never commit `config/config.yaml`, credentials, private hostnames, device
  serial numbers, or personal Home Assistant entity IDs.
- Use generic example identifiers such as `computer-one`, `computer-two`, and
  `mqtt.example.test`.
- Document new configuration fields and preserve existing device
  configurations when practical.
- Run the complete test and static-check commands before submitting.

Third-party binaries and dependencies require a license and redistribution
review before they can be added to source or release artifacts.
