import unittest
from unittest.mock import Mock

from desk_controller.desktop_agent.audio.windows import WindowsAudioDriver


class FakeWindowsDevice:
    def __init__(self, device_id, name, muted=False):
        self.id = device_id
        self.FriendlyName = name
        self.EndpointVolume = Mock()
        self.EndpointVolume.GetMute.return_value = muted


class WindowsAudioDriverTests(unittest.TestCase):
    def setUp(self):
        self.speakers = FakeWindowsDevice("speakers-id", "Desk Speakers")
        self.headphones = FakeWindowsDevice(
            "headphones-id",
            "USB Headphones",
            muted=True,
        )
        self.backend = Mock()
        self.backend.default_output.return_value = self.headphones
        self.backend.output_devices.return_value = [
            self.speakers,
            self.headphones,
        ]
        self.driver = WindowsAudioDriver(backend=self.backend)

    def test_audio_state_uses_live_core_audio_devices(self):
        state = self.driver.get_audio_state()

        self.assertEqual(state.active_device, "USB Headphones")
        self.assertTrue(state.is_muted)
        self.assertEqual(
            [
                (device.id, device.name, device.is_default)
                for device in state.available_devices
            ],
            [
                ("speakers-id", "Desk Speakers", False),
                ("headphones-id", "USB Headphones", True),
            ],
        )

    def test_output_can_be_selected_by_exact_name_or_partial_name(self):
        self.assertTrue(self.driver.set_output_device("Desk Speakers"))
        self.backend.set_default_output.assert_called_once_with("speakers-id")

        self.backend.set_default_output.reset_mock()
        self.assertTrue(self.driver.set_output_device("headphones"))
        self.backend.set_default_output.assert_called_once_with("headphones-id")

    def test_unknown_output_fails_closed(self):
        self.assertFalse(self.driver.set_output_device("Missing Device"))
        self.backend.set_default_output.assert_not_called()

    def test_mute_uses_default_endpoint_volume(self):
        self.assertTrue(self.driver.set_muted(True))
        self.backend.set_muted.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
