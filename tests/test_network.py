import unittest
from unittest.mock import Mock, patch

from desk_controller.core.network import get_lan_ip


class NetworkIdentityTests(unittest.TestCase):
    def test_route_selected_ipv4_is_reported(self):
        fake_socket = Mock()
        fake_socket.__enter__ = Mock(return_value=fake_socket)
        fake_socket.__exit__ = Mock(return_value=False)
        fake_socket.getsockname.return_value = ("192.168.50.24", 50123)

        with patch(
            "desk_controller.core.network.socket.socket",
            return_value=fake_socket,
        ):
            address = get_lan_ip("mqtt.internal", 1883)

        self.assertEqual(address, "192.168.50.24")
        fake_socket.connect.assert_called_once_with(("mqtt.internal", 1883))


if __name__ == "__main__":
    unittest.main()
