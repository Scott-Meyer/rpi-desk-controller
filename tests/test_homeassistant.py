import unittest
from unittest.mock import Mock, patch

import requests

from desk_controller.pi_controller.integrations.homeassistant import (
    HomeAssistantClient,
)


class HomeAssistantClientTests(unittest.TestCase):
    def setUp(self):
        self.client = HomeAssistantClient(
            "http://homeassistant.local:8123",
            "secret-token",
        )

    @patch("desk_controller.pi_controller.integrations.homeassistant.requests.get")
    def test_connection_status_reports_authenticated_api(self, request):
        request.return_value = Mock(status_code=200)

        status = self.client.connection_status()

        self.assertTrue(status["authenticated"])
        self.assertEqual(status["status"], "authenticated")
        request.assert_called_once_with(
            "http://homeassistant.local:8123/api/",
            headers=self.client.headers,
            timeout=3,
        )

    @patch("desk_controller.pi_controller.integrations.homeassistant.requests.get")
    def test_connection_status_reports_rejected_token(self, request):
        request.return_value = Mock(status_code=401)

        status = self.client.connection_status()

        self.assertFalse(status["authenticated"])
        self.assertEqual(status["status"], "authentication_failed")
        self.assertEqual(status["http_status"], 401)

    @patch("desk_controller.pi_controller.integrations.homeassistant.requests.get")
    def test_connection_status_reports_unreachable_api(self, request):
        request.side_effect = requests.ConnectionError("offline")

        status = self.client.connection_status()

        self.assertFalse(status["authenticated"])
        self.assertEqual(status["status"], "unreachable")
        self.assertNotIn("offline", status["detail"])


if __name__ == "__main__":
    unittest.main()
