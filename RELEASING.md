# Release and publication checklist

This checklist separates making the source repository public from publishing
desktop binaries. Neither operation should be inferred from ordinary code
changes.

## Public repository

1. Confirm the default branch contains only intended public history, has no
   personal configuration or credentials, and is either unsigned or signed
   with a personal key.
2. Confirm CI passes on Linux, macOS, and Windows.
3. Change repository visibility only after an explicit publication decision.
4. Immediately enable private vulnerability reporting:

   ```bash
   gh api \
     --method PUT \
     repos/Scott-Meyer/rpi-desk-controller/private-vulnerability-reporting
   gh api \
     repos/Scott-Meyer/rpi-desk-controller/private-vulnerability-reporting \
     --jq '.enabled'
   ```

   The second command must print `true`. GitHub only makes this setting
   available for public repositories.
5. Enable secret scanning and push protection, then configure default-branch
   protection or a repository ruleset.
6. Add the repository description and topics, and verify the security-reporting
   link in `SECURITY.md` while signed out.

## Desktop release

1. Update the package version and replace `Unreleased` in `CHANGELOG.md` with
   the release date.
2. Run **Build & Release Desktop Agents** manually against the intended commit.
   Manual runs build and upload both platform artifacts but do not create a
   GitHub release.
3. Download the Windows executable and macOS disk image from that workflow run.
   Smoke-test installation, first-run MQTT setup, reconnect behavior, tray
   controls, and removal on the supported operating systems.
4. Verify the generated third-party license inventory is present in each
   application.
5. Document that Windows artifacts are unsigned and macOS artifacts are
   ad-hoc-signed and not notarized until production signing is configured.
6. Create the matching version tag only after the checks above. Pushing a
   `v*` tag publishes the release and its SHA-256 checksums.
