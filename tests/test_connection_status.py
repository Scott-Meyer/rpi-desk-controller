import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from desk_controller.pi_controller.main import DeskControllerApp


class ControllerConnectionStatusTests(unittest.TestCase):
    def test_live_status_combines_mqtt_session_and_home_assistant_auth(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.config = {
            "mqtt": {
                "mode": "external",
                "username": "desk-user",
                "password": "secret",  # pragma: allowlist secret
            }
        }
        controller.mqtt = SimpleNamespace(
            is_connected=True,
            broker="homeassistant.local",
            port=1883,
        )
        controller.homeassistant_enabled = True
        controller.ha = SimpleNamespace(
            base_url="http://homeassistant.local:8123",
            connection_status=Mock(
                return_value={
                    "status": "authenticated",
                    "authenticated": True,
                    "http_status": 200,
                    "detail": "API reachable and access token accepted.",
                }
            ),
        )

        status = controller._connection_status()

        self.assertTrue(status["mqtt"]["connected"])
        self.assertEqual(status["mqtt"]["mode"], "external")
        self.assertEqual(status["mqtt"]["recovery_count"], 0)
        self.assertTrue(status["homeassistant"]["authenticated"])
        self.assertNotIn("secret", str(status))

    def test_disabled_home_assistant_does_not_make_an_api_request(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.config = {"mqtt": {"mode": "local", "username": ""}}
        controller.mqtt = SimpleNamespace(
            is_connected=False,
            broker="127.0.0.1",
            port=1883,
        )
        controller.homeassistant_enabled = False
        controller.ha = SimpleNamespace(
            base_url="http://homeassistant.local:8123",
            connection_status=Mock(),
        )

        status = controller._connection_status()

        self.assertEqual(
            status["homeassistant"]["status"],
            "disabled",
        )
        controller.ha.connection_status.assert_not_called()


if __name__ == "__main__":
    unittest.main()
