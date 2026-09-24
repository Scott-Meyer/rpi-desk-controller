"""Provenance of the source currently running, independent of release numbers.

The SSH deployer stamps its rsynced source. Without that stamp provenance is
unknown: a copied source tree may sit beside unrelated or stale Git metadata.
"""

import json
import re
from pathlib import Path
from typing import Any

import requests

SOURCE_STAMP = Path(__file__).with_name("_source_version.json")
GITHUB_COMMITS_URL = (
    "https://api.github.com/repos/Scott-Meyer/rpi-desk-controller/commits"
)
GITHUB_COMMIT_URL = "https://github.com/Scott-Meyer/rpi-desk-controller/commit/"
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _valid_revision(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _running_source() -> dict:
    """Resolve deployed source identity once, when the service starts."""
    if SOURCE_STAMP.exists():
        try:
            stamped = json.loads(SOURCE_STAMP.read_text(encoding="utf-8"))
            if _valid_revision(stamped.get("commit")) and isinstance(
                stamped.get("dirty"), bool
            ):
                return {
                    "commit": stamped["commit"],
                    "dirty": stamped["dirty"],
                    "provenance": "deployed source",
                }
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        # A malformed deploy stamp is not evidence of a particular commit.
        return {"commit": None, "dirty": None, "provenance": "unknown"}

    return {"commit": None, "dirty": None, "provenance": "unknown"}


RUNNING_SOURCE = _running_source()


def github_head() -> dict:
    """Get GitHub's default-branch head; failed or invalid lookups stay unknown."""
    try:
        response = requests.get(
            GITHUB_COMMITS_URL,
            params={"per_page": 1},
            headers={"Accept": "application/vnd.github+json"},
            timeout=5,
        )
        response.raise_for_status()
        revisions = response.json()
        if not isinstance(revisions, list) or not revisions:
            raise ValueError("GitHub returned no commits")
        commit = revisions[0]["sha"]
        if not _valid_revision(commit):
            raise ValueError("GitHub returned an invalid revision")
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError):
        return {"commit": None, "url": None}
    return {"commit": commit, "url": f"{GITHUB_COMMIT_URL}{commit}"}


def version_status(source: dict, latest: dict) -> str:
    """Only pristine identical source can be reported as GitHub-current."""
    if not source.get("commit") or not latest.get("commit"):
        return "unknown"
    if source.get("dirty"):
        return "modified"
    if source["commit"] != latest["commit"]:
        return "different"
    return "current"
