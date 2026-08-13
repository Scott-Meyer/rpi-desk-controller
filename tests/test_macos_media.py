import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_controller.desktop_agent.media import MacOSMediaDriver


class MacOSMediaDriverTests(unittest.TestCase):
    def make_driver(self, info):
        driver = object.__new__(MacOSMediaDriver)
        driver._queue = object()
        driver._get_now_playing_info = lambda _queue, callback: callback(info)
        driver._send_command = Mock(return_value=True)
        driver._get_system_now_playing = Mock(return_value=None)
        return driver

    def test_now_playing_metadata_maps_to_media_state(self):
        driver = self.make_driver(
            {
                "kMRMediaRemoteNowPlayingInfoTitle": "Blue Monday",
                "kMRMediaRemoteNowPlayingInfoArtist": "New Order",
                "kMRMediaRemoteNowPlayingInfoPlaybackRate": 1,
            }
        )

        state = driver.get_now_playing()

        self.assertTrue(state.available)
        self.assertEqual(state.title, "Blue Monday")
        self.assertEqual(state.artist, "New Order")
        self.assertTrue(state.is_playing)

    def test_empty_metadata_reports_no_media(self):
        state = self.make_driver({}).get_now_playing()

        self.assertFalse(state.available)
        self.assertFalse(state.is_playing)

    def test_toggle_sends_native_media_remote_command(self):
        driver = self.make_driver({})

        self.assertTrue(driver.toggle_play_pause())
        driver._send_command.assert_called_once_with(2, None)

    @patch("desk_controller.desktop_agent.media.subprocess.run")
    def test_system_now_playing_request_reads_any_active_player(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"title":"It’s Alright","artist":"Pet Shop Boys","playing":true}\n'
            ),
            stderr="",
        )

        state = MacOSMediaDriver._get_system_now_playing()

        self.assertTrue(state.available)
        self.assertEqual(state.title, "It’s Alright")
        self.assertEqual(state.artist, "Pet Shop Boys")
        self.assertTrue(state.is_playing)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:3],
            [
                "/usr/bin/osascript",
                "-l",
                "JavaScript",
            ],
        )
        self.assertIn("MRNowPlayingRequest", command[-1])

    @patch("desk_controller.desktop_agent.media.subprocess.run")
    def test_system_now_playing_request_reports_no_session(self, run):
        run.return_value = SimpleNamespace(
            returncode=0,
            stdout="null\n",
            stderr="",
        )

        state = MacOSMediaDriver._get_system_now_playing()

        self.assertFalse(state.available)
        self.assertFalse(state.is_playing)


if __name__ == "__main__":
    unittest.main()
