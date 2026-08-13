import platform
import unittest
from unittest.mock import Mock

from desk_controller.core.models import AudioDevice, AudioState
from desk_controller.desktop_agent.audio.macos import MacOSAudioDriver


class MacOSAudioDriverTests(unittest.TestCase):
    def test_driver_uses_injected_core_audio_backend(self):
        expected = AudioState(
            active_device="USB DAC",
            available_devices=[
                AudioDevice(
                    id="persistent-device-uid",
                    name="USB DAC",
                    is_default=True,
                )
            ],
        )
        backend = Mock()
        backend.get_audio_state.return_value = expected
        driver = MacOSAudioDriver(backend=backend)

        self.assertEqual(driver.get_audio_state(), expected)
        backend.get_audio_state.assert_called_once_with()

    def test_backend_failure_never_returns_fabricated_devices(self):
        backend = Mock()
        backend.get_audio_state.side_effect = OSError("CoreAudio unavailable")
        driver = MacOSAudioDriver(backend=backend)

        state = driver.get_audio_state()

        self.assertIsNone(state.active_device)
        self.assertEqual(state.available_devices, [])

    def test_switching_delegates_to_core_audio_backend(self):
        backend = Mock()
        backend.set_output_device.return_value = True
        driver = MacOSAudioDriver(backend=backend)

        self.assertTrue(driver.set_output_device("persistent-device-uid"))
        backend.set_output_device.assert_called_once_with("persistent-device-uid")

    def test_mute_toggle_delegates_to_core_audio_backend(self):
        backend = Mock()
        backend.get_audio_state.return_value = AudioState(is_muted=False)
        backend.set_muted.return_value = True
        driver = MacOSAudioDriver(backend=backend)

        self.assertTrue(driver.toggle_mute())

        backend.set_muted.assert_called_once_with(True)

    @unittest.skipUnless(platform.system() == "Darwin", "requires macOS CoreAudio")
    def test_real_backend_reports_real_devices_without_homebrew_cli(self):
        state = MacOSAudioDriver().get_audio_state()

        self.assertTrue(state.available_devices)
        self.assertNotIn(
            ("1", "Workstation B Pro Speakers"),
            [(device.id, device.name) for device in state.available_devices],
        )
        self.assertNotIn(
            ("2", "AirPods Max"),
            [(device.id, device.name) for device in state.available_devices],
        )


if __name__ == "__main__":
    unittest.main()
