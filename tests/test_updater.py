import unittest
from unittest.mock import Mock, patch

import requests

from desk_controller.desktop_agent.updater import GitHubReleaseUpdater


class GitHubReleaseUpdaterTests(unittest.TestCase):
    @patch("desk_controller.desktop_agent.updater.requests.get")
    def test_new_release_returns_canonical_manual_review_url(self, get):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "tag_name": "v99.0.0",
            "body": "Release notes",
            "html_url": "https://attacker.invalid/release",
        }
        get.return_value = response

        result = GitHubReleaseUpdater.check_for_updates()

        self.assertTrue(result["update_available"])
        self.assertEqual(result["latest_version"], "v99.0.0")
        self.assertEqual(
            result["release_url"],
            "https://github.com/Scott-Meyer/rpi-desk-controller/releases/tag/v99.0.0",
        )
        get.assert_called_once_with(
            GitHubReleaseUpdater.API_URL,
            headers={"Accept": "application/vnd.github+json"},
            timeout=5,
        )

    @patch("desk_controller.desktop_agent.updater.requests.get")
    def test_network_failure_is_reported_as_no_update(self, get):
        get.side_effect = requests.ConnectionError("offline")

        self.assertEqual(
            GitHubReleaseUpdater.check_for_updates(),
            {"update_available": False},
        )

    @patch("desk_controller.desktop_agent.updater.webbrowser.open")
    def test_open_release_never_executes_the_download(self, open_browser):
        open_browser.return_value = True

        self.assertTrue(GitHubReleaseUpdater.open_release("v2.0.0"))

        open_browser.assert_called_once_with(
            "https://github.com/Scott-Meyer/rpi-desk-controller/releases/tag/v2.0.0"
        )


if __name__ == "__main__":
    unittest.main()
