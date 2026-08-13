import unittest
from datetime import datetime

from PIL import Image, ImageDraw

from desk_controller.pi_controller.drivers.streamdeck_mgr import StreamDeckManager
from desk_controller.pi_controller.streamdeck_layout import (
    configured_streamdeck_buttons,
    configured_usb_ports,
    datetime_button_label,
    default_streamdeck_buttons,
)


class StreamDeckLayoutTests(unittest.TestCase):
    def test_pending_border_is_dotted_yellow(self):
        image = Image.new("RGB", (20, 20), color=(0, 0, 0))
        draw = ImageDraw.Draw(image)

        StreamDeckManager._draw_dotted_border(
            draw,
            width=20,
            height=20,
            color=(255, 196, 0),
        )

        self.assertEqual(image.getpixel((2, 2)), (255, 196, 0))
        self.assertEqual(image.getpixel((7, 2)), (0, 0, 0))

    def test_empty_config_uses_generic_kvm_only_layout(self):
        buttons = configured_streamdeck_buttons({})

        self.assertEqual(list(buttons), [0])
        self.assertEqual(buttons[0]["action_type"], "kvm_toggle")

    def test_layout_copies_are_independent(self):
        first = default_streamdeck_buttons()
        second = default_streamdeck_buttons()

        first[0]["label"] = "Changed"

        self.assertEqual(second[0]["label"], "HOST\nPC")

    def test_old_audio_and_scene_config_is_migrated(self):
        buttons = configured_streamdeck_buttons(
            {
                "audio_devices": {
                    1: {
                        "name": "DAC",
                        "label": "Desk DAC",
                        "icon": "headphones",
                    }
                },
                "streamdeck": {
                    "keys": {
                        5: {
                            "type": "ha_scene",
                            "scene": "scene.work",
                            "off_automation": "automation.off",
                            "label": "WORK",
                            "icon": "sun",
                        }
                    }
                },
            }
        )

        self.assertEqual(buttons[1]["action_type"], "audio_output")
        self.assertEqual(buttons[1]["target"], "DAC")
        self.assertEqual(buttons[5]["action_type"], "ha_scene")
        self.assertEqual(buttons[5]["target"], "scene.work")
        self.assertEqual(buttons[5]["off_target"], "automation.off")

    def test_usb_port_names_always_cover_the_eight_physical_ports(self):
        ports = configured_usb_ports({"usb_hub": {"ports": {"2": {"name": "Camera"}}}})

        self.assertEqual(list(ports), list(range(8)))
        self.assertEqual(ports[2]["name"], "Camera")
        self.assertEqual(ports[7]["name"], "USB Port 7")

    def test_date_and_time_buttons_use_compact_two_line_labels(self):
        now = datetime(2026, 7, 28, 9, 5)

        self.assertEqual(datetime_button_label("current_time", now), "9:05\nAM")
        self.assertEqual(datetime_button_label("current_date", now), "Tue\nJul 28")


if __name__ == "__main__":
    unittest.main()
