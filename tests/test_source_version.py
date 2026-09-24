"""The reported source identity must not turn uncertainty into 'up to date'."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from desk_controller import source_version


class SourceVersionTests(unittest.TestCase):
    def test_deployment_stamp_identifies_actual_transferred_source(self):
        revision = "b" * 40
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "stamp.json"
            stamp.write_text(json.dumps({"commit": revision, "dirty": True}))
            with patch.object(source_version, "SOURCE_STAMP", stamp):
                running = source_version._running_source()

        self.assertEqual(
            running,
            {
                "commit": revision,
                "dirty": True,
                "provenance": "deployed source",
            },
        )
        self.assertEqual(
            source_version.version_status(running, {"commit": revision}),
            "modified",
        )

    def test_missing_stamp_is_unknown_even_when_a_git_checkout_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(
                source_version, "SOURCE_STAMP", Path(tmp) / "missing.json"
            ):
                self.assertEqual(
                    source_version._running_source(),
                    {"commit": None, "dirty": None, "provenance": "unknown"},
                )

    def test_invalid_stamp_is_not_replaced_with_unrelated_git_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "stamp.json"
            stamp.write_text('{"commit": "no", "dirty": false}')
            with patch.object(source_version, "SOURCE_STAMP", stamp):
                self.assertEqual(source_version._running_source()["commit"], None)

    def test_head_difference_is_not_assumed_to_be_behind(self):
        local = {"commit": "c" * 40, "dirty": False}
        self.assertEqual(
            source_version.version_status(local, {"commit": "d" * 40}), "different"
        )
        self.assertEqual(
            source_version.version_status(local, {"commit": None}), "unknown"
        )
        self.assertEqual(
            source_version.version_status(local, {"commit": "c" * 40}), "current"
        )

    @patch("desk_controller.source_version.requests.get")
    def test_github_head_uses_default_branch_and_rejects_unverified_responses(
        self, get
    ):
        revision = "e" * 40
        response = Mock()
        response.json.return_value = [{"sha": revision}]
        get.return_value = response
        self.assertEqual(
            source_version.github_head(),
            {
                "commit": revision,
                "url": f"https://github.com/Scott-Meyer/rpi-desk-controller/commit/{revision}",
            },
        )
        get.assert_called_once_with(
            source_version.GITHUB_COMMITS_URL,
            params={"per_page": 1},
            headers={"Accept": "application/vnd.github+json"},
            timeout=5,
        )
        get.side_effect = requests.Timeout("offline")
        self.assertEqual(source_version.github_head(), {"commit": None, "url": None})
