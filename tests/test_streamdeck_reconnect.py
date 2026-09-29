"""A newly attached deck becomes the current rendered and announced device."""

import unittest
from unittest.mock import Mock, patch

from desk_controller.pi_controller.drivers import streamdeck_mgr
from desk_controller.pi_controller.drivers.streamdeck_mgr import StreamDeckManager
from desk_controller.pi_controller.main import DeskControllerApp


class ControllerDeckReconnectTests(unittest.TestCase):
    def make_controller(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.streamdeck = Mock()
        controller._update_sd_keys = Mock()
        controller._publish_streamdeck_layout = Mock()
        controller._publish_streamdeck_state = Mock()
        controller.register_ha_streamdeck_discovery = Mock()
        controller.homeassistant_enabled = True
        return controller

    def test_replug_repaints_and_announces_once_per_transition(self):
        controller = self.make_controller()
        controller.streamdeck.refresh_connection.side_effect = [False, True, False]
        controller.streamdeck.layout.return_value = (2, 3)
        self.assertFalse(controller._refresh_streamdeck())
        self.assertTrue(controller._refresh_streamdeck())
        self.assertFalse(controller._refresh_streamdeck())
        controller._update_sd_keys.assert_called_once()
        controller._publish_streamdeck_layout.assert_called_once()
        controller._publish_streamdeck_state.assert_called_once()
        controller.register_ha_streamdeck_discovery.assert_called_once()

    def test_unplug_announces_disconnection_without_adding_key_triggers(self):
        controller = self.make_controller()
        controller.streamdeck.refresh_connection.return_value = True
        controller.streamdeck.layout.return_value = None
        self.assertTrue(controller._refresh_streamdeck())
        controller._publish_streamdeck_layout.assert_called_once()
        controller._publish_streamdeck_state.assert_called_once()
        controller.register_ha_streamdeck_discovery.assert_not_called()

    def test_accepted_six_key_press_keeps_its_physical_coordinates_after_detach(self):
        controller = DeskControllerApp.__new__(DeskControllerApp)
        controller.buttons = {4: {"enabled": False, "label": "inactive"}}
        controller._get_active_hostname = lambda: None
        controller._publish_streamdeck_state = Mock()
        controller.mqtt = Mock()
        manager = StreamDeckManager()
        controller.streamdeck = manager
        deck = Mock()
        deck.is_open.return_value = True
        deck.connected.return_value = True
        deck.key_layout.return_value = (2, 3)
        manager.deck = deck

        def accepted_press(press):
            deck.connected.return_value = False
            manager.refresh_connection()
            controller._handle_key_press(press)

        manager.key_callback = accepted_press
        with (
            patch.object(streamdeck_mgr, "STREAMDECK_LIB_AVAILABLE", True),
            patch.object(
                streamdeck_mgr,
                "DeviceManager",
                lambda: Mock(enumerate=lambda: []),
                create=True,
            ),
        ):
            manager._on_key_change(deck, 4, True)
        self.assertIsNone(manager.layout())
        _, event = controller.mqtt.publish.call_args_list[1].args
        self.assertEqual((event["row"], event["column"]), (1, 1))
        self.assertEqual(event["key"], 4)

    def test_retained_layout_reports_absence_separately_from_preview_geometry(self):
        controller = self.make_controller()
        controller._publish_streamdeck_layout = (
            DeskControllerApp._publish_streamdeck_layout.__get__(controller)
        )
        controller._deck_dimensions = lambda: (3, 5)
        controller.streamdeck.layout.return_value = None
        controller.physical_buttons = {}
        controller.buttons = {}
        controller.config = {"server": {"port": 8080}}
        controller._lan_ip = "192.168.1.125"
        controller.mqtt = Mock()
        controller._publish_streamdeck_layout()
        topic, payload = controller.mqtt.publish.call_args.args
        self.assertEqual(topic, f"{controller.STREAMDECK_TOPIC}/layout")
        self.assertFalse(payload["device_detected"])
        self.assertEqual((payload["rows"], payload["columns"]), (3, 5))
        controller.streamdeck.layout.return_value = (2, 3)
        controller._publish_streamdeck_layout()
        self.assertTrue(controller.mqtt.publish.call_args.args[1]["device_detected"])


if __name__ == "__main__":
    unittest.main()
