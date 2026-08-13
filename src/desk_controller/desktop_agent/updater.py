"""Safe GitHub Releases update checks for the desktop agent."""

import logging
import webbrowser
from urllib.parse import quote

import requests
from packaging.version import InvalidVersion, Version

from desk_controller import __version__

logger = logging.getLogger(__name__)


class GitHubReleaseUpdater:
    REPO_OWNER = "Scott-Meyer"
    REPO_NAME = "rpi-desk-controller"
    CURRENT_VERSION = f"v{__version__}"
    API_URL = f"https://api.github.com/repos/{REPO_OWNER}/{REPO_NAME}/releases/latest"

    @classmethod
    def release_url(cls, tag: str) -> str:
        safe_tag = quote(tag, safe="")
        return (
            f"https://github.com/{cls.REPO_OWNER}/{cls.REPO_NAME}/"
            f"releases/tag/{safe_tag}"
        )

    @classmethod
    def check_for_updates(cls) -> dict:
        """Return update metadata without downloading or executing code."""
        try:
            response = requests.get(
                cls.API_URL,
                headers={"Accept": "application/vnd.github+json"},
                timeout=5,
            )
            if response.status_code != 200:
                logger.warning(
                    "GitHub Releases check returned status %s",
                    response.status_code,
                )
                return {"update_available": False}

            data = response.json()
            latest_tag = str(data.get("tag_name", "")).strip()
            current = Version(cls.CURRENT_VERSION.lstrip("v"))
            latest = Version(latest_tag.lstrip("v"))
        except (InvalidVersion, TypeError, ValueError):
            logger.exception("GitHub returned an invalid release version")
            return {"update_available": False}
        except requests.RequestException:
            logger.exception("GitHub release check failed")
            return {"update_available": False}

        if latest <= current:
            return {
                "update_available": False,
                "latest_version": latest_tag,
            }

        return {
            "update_available": True,
            "latest_version": latest_tag,
            "release_notes": data.get("body", ""),
            "release_url": cls.release_url(latest_tag),
        }

    @classmethod
    def open_release(cls, tag: str) -> bool:
        """Open the canonical GitHub release page for manual verification."""
        if not tag:
            return False
        return bool(webbrowser.open(cls.release_url(tag)))
