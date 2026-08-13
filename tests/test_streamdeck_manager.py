import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from desk_controller.pi_controller.drivers import streamdeck_mgr
from desk_controller.pi_controller.drivers.streamdeck_mgr import StreamDeckManager


class FakeDeck:
    def __init__(self):
        self.image = None

    def key_image_format(self):
        return {"size": (72, 72)}

    def set_key_image(self, _key, image):
        self.image = image

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakePILHelper:
    @staticmethod
    def to_native_format(_deck, image):
        return image


class StreamDeckManagerTests(unittest.TestCase):
    def test_time_display_uses_the_safe_area_and_accent_pill(self):
        image = Image.new("RGB", (72, 72), color=(10, 10, 14))
        draw = ImageDraw.Draw(image)
        accent = (0, 200, 255)

        StreamDeckManager._draw_datetime_display(
            draw,
            72,
            72,
            "9:05\nAM",
            "current_time",
            accent,
            (255, 255, 255),
        )

        changed_pixels = [
            (x, y)
            for y in range(72)
            for x in range(72)
            if image.getpixel((x, y)) != (10, 10, 14)
        ]
        xs = [x for x, _ in changed_pixels]
        ys = [y for _, y in changed_pixels]
        self.assertGreaterEqual(min(xs), 11)
        self.assertLessEqual(max(xs), 60)
        self.assertGreaterEqual(min(ys), 11)
        self.assertLessEqual(max(ys), 60)
        self.assertIn(accent, image.getdata())

    def test_date_display_draws_accented_weekday_and_large_date(self):
        image = Image.new("RGB", (72, 72), color=(10, 10, 14))
        draw = ImageDraw.Draw(image)

        StreamDeckManager._draw_datetime_display(
            draw,
            72,
            72,
            "Tue\nJul 28",
            "current_date",
            (0, 200, 255),
            (255, 255, 255),
        )

        top_half = image.crop((0, 0, 72, 30))
        bottom_half = image.crop((0, 30, 72, 72))
        self.assertIsNotNone(top_half.getbbox())
        self.assertIsNotNone(bottom_half.getbbox())
        self.assertIn((0, 200, 255), top_half.getdata())
        self.assertIn((255, 255, 255), bottom_half.getdata())

    def test_all_display_styles_reserve_fifteen_percent_at_every_edge(self):
        manager = StreamDeckManager()
        manager.deck = FakeDeck()
        cases = (
            ("button", "LONG LABEL\nSECOND LINE", "wireless_headphones"),
            ("current_time", "12:59\nPM", "none"),
            ("current_date", "Wed\nSep 30", "none"),
        )

        with (
            patch.object(streamdeck_mgr, "STREAMDECK_LIB_AVAILABLE", True),
            patch.object(
                streamdeck_mgr,
                "PILHelper",
                FakePILHelper,
                create=True,
            ),
        ):
            for display_style, label, icon in cases:
                with self.subTest(display_style=display_style):
                    manager.render_scene_key(
                        key=0,
                        label=label,
                        icon_type=icon,
                        display_style=display_style,
                    )

                    image = manager.deck.image
                    edge_pixels = {
                        image.getpixel((x, y))
                        for y in range(72)
                        for x in range(72)
                        if x < 11 or x >= 61 or y < 11 or y >= 61
                    }
                    self.assertLessEqual(
                        edge_pixels,
                        {
                            (10, 10, 14),
                            (40, 40, 50),
                        },
                    )


if __name__ == "__main__":
    unittest.main()
