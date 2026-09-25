import unittest
from unittest.mock import Mock, patch

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
    def test_layout_uses_connected_hardware_instead_of_fifteen_key_assumption(self):
        manager = StreamDeckManager()
        self.assertIsNone(manager.layout())
        self.assertIsNone(manager.key_image_size())
        manager.deck = Mock()
        manager.deck.key_layout.return_value = (2, 3)
        manager.deck.key_count.return_value = 6
        self.assertEqual(manager.layout(), (2, 3))
        manager.deck.key_image_format.return_value = {"size": (80, 80)}
        self.assertEqual(manager.key_image_size(), (80, 80))

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
        self.assertTrue(any(b > 150 for r, g, b in top_half.getdata()))
        self.assertTrue(
            any(r > 200 and g > 200 and b > 200 for r, g, b in bottom_half.getdata())
        )

    def _render_status(
        self, control, observed, next_action, *, phase="ready", target=None
    ):
        manager = StreamDeckManager()
        manager.deck = FakeDeck()
        with (
            patch.object(streamdeck_mgr, "STREAMDECK_LIB_AVAILABLE", True),
            patch.object(streamdeck_mgr, "PILHelper", FakePILHelper, create=True),
        ):
            manager.deck.key_image_format = lambda: {"size": (80, 80)}
            manager.render_status_key(
                0,
                control,
                observed,
                next_action,
                phase=phase,
                target=target,
            )
        return manager.deck.image

    def test_custom_ha_button_label_does_not_break_artwork_or_hide_led(self):
        on = StreamDeckManager.status_image("Desk Light", "LED ON", "PRESS")
        unknown = StreamDeckManager.status_image(
            "Desk Light", "LED ?", "PRESS", phase="unknown"
        )
        self.assertEqual(on.size, (80, 80))
        self.assertNotEqual(
            on.crop((30, 42, 50, 62)).tobytes(),
            unknown.crop((30, 42, 50, 62)).tobytes(),
        )

    def test_web_image_is_exactly_the_artwork_sent_to_hardware(self):
        visual = {
            "control": "AC",
            "observed": "SLEEP",
            "next_action": "→ COLD",
            "phase": "pending",
            "target": "… COLD",
        }
        physical = self._render_status(**visual)
        web = StreamDeckManager.status_image(**visual)
        self.assertEqual(physical.size, web.size)
        self.assertEqual(physical.tobytes(), web.tobytes())

    def test_status_content_stays_inside_80_pixel_safe_area(self):
        background = (8, 12, 20)
        for control, observed, action in (
            ("HOST", "PC 2", "→ PC 1"),
            ("AC", "SLEEP", "→ COLD"),
            ("SHADES", "MIXED", "↓ CLOSE"),
            ("BT1", "LED ON", "PRESS"),
            ("BT2", "LED OFF", "PRESS"),
            ("BT3", "LED ?", "PRESS"),
        ):
            for phase in ("ready", "pending", "ack", "error", "unknown"):
                with self.subTest(control=control, phase=phase):
                    image = self._render_status(
                        control,
                        observed,
                        action,
                        phase=phase,
                        target="PC 1",
                    )
                    # The bezel border has a dedicated channel outside the 12px inset.
                    for y in range(80):
                        for x in range(80):
                            if x < 12 or x >= 68 or y < 12 or y >= 68:
                                pixel = image.getpixel((x, y))
                                if pixel != background:
                                    self.assertTrue(
                                        x < 9 or x >= 71 or y < 9 or y >= 71,
                                        (control, phase, x, y),
                                    )
                    self.assertNotEqual(image.crop((12, 12, 68, 68)).getbbox(), None)

    def test_status_phases_preserve_observation_and_distinguish_intent(self):
        ready = self._render_status("HOST", "PC 2", "→ PC 1")
        pending = self._render_status(
            "HOST",
            "PC 2",
            "→ PC 1",
            phase="pending",
            target="PC 1",
        )
        pending_other_target = self._render_status(
            "HOST",
            "PC 2",
            "→ PC 1",
            phase="pending",
            target="PC 2",
        )
        error = self._render_status("HOST", "PC 2", "RETRY", phase="error")
        unknown = self._render_status("HOST", None, "→ PC 1")
        unknown_without_action = self._render_status("HOST", None, None)

        # A request highlights responsiveness, never lights the other host as
        # though the picture has already moved. The border, not text, says pending.
        left = (23, 28, 38, 46)
        right = (42, 28, 57, 46)

        def brightest(image, area):
            return max(sum(pixel) for pixel in image.crop(area).getdata())

        self.assertGreater(brightest(ready, right), brightest(ready, left))
        self.assertGreater(brightest(pending, right), brightest(ready, right))
        self.assertEqual(
            pending.crop(left).tobytes(), pending_other_target.crop(left).tobytes()
        )
        self.assertEqual(
            pending.crop(right).tobytes(), pending_other_target.crop(right).tobytes()
        )
        self.assertLess(brightest(unknown, right), brightest(ready, right))
        self.assertEqual(ready.getpixel((40, 2)), (8, 12, 20))
        self.assertIn((255, 196, 0), pending.crop((0, 0, 80, 9)).getdata())
        self.assertEqual(error.getpixel((40, 2)), (255, 77, 88))
        self.assertEqual(unknown.getpixel((40, 2)), (8, 12, 20))
        self.assertEqual(unknown.tobytes(), unknown_without_action.tobytes())
        # Font rasterization varies by OS; the error glyph still reads red.
        self.assertTrue(
            any(
                red > green + 30 and red > blue + 30
                for red, green, blue in error.crop((50, 8, 76, 30)).getdata()
            )
        )

    def test_keypad_glow_tracks_led_and_unknown_is_not_off(self):
        on = self._render_status("BT1", "LED ON", "PRESS")
        off = self._render_status("BT1", "LED OFF", "PRESS")
        unknown = self._render_status("BT1", "LED ?", "PRESS")
        pending = self._render_status("BT1", "LED OFF", "PRESS", phase="pending")
        ack = self._render_status("BT1", "LED OFF", "PRESS", phase="ack")
        failed = self._render_status("BT1", "LED OFF", "PRESS", phase="error")
        self.assertGreater(sum(on.getpixel((40, 37))), sum(off.getpixel((40, 37))))
        self.assertNotEqual(
            unknown.crop((30, 25, 50, 48)).tobytes(),
            off.crop((30, 25, 50, 48)).tobytes(),
        )
        self.assertEqual(pending.getpixel((40, 37)), off.getpixel((40, 37)))
        self.assertIn((102, 230, 151), ack.crop((56, 12, 68, 25)).getdata())
        self.assertTrue(
            any(
                red > green + 30 and red > blue + 30
                for red, green, blue in failed.crop((50, 8, 76, 30)).getdata()
            )
        )
        bulbs = [self._render_status(f"BT{i}", "LED ON", "PRESS") for i in (1, 2, 3)]
        self.assertLess(
            sum(bulbs[0].getpixel((40, 37))), sum(bulbs[1].getpixel((40, 37)))
        )
        self.assertLess(
            sum(bulbs[1].getpixel((40, 37))), sum(bulbs[2].getpixel((40, 37)))
        )
        # The three programmed buttons have different *engraved* art, not
        # invented dimmer commands or an assertion about the room's lights.
        self.assertNotEqual(bulbs[0].getpixel((34, 36)), bulbs[0].getpixel((46, 36)))
        self.assertEqual(bulbs[1].getpixel((34, 36)), bulbs[1].getpixel((46, 36)))
        self.assertEqual(bulbs[0].getpixel((40, 18)), (8, 12, 20))
        self.assertEqual(bulbs[1].getpixel((40, 18)), (8, 12, 20))
        self.assertNotEqual(bulbs[2].getpixel((40, 18)), (8, 12, 20))

    def test_status_icons_depict_actual_state_in_each_control(self):
        for control, states in (
            ("HOST", ("PC 1", "PC 2", "PI")),
            ("AC", ("COLD", "SLEEP")),
            ("SHADES", ("OPEN", "CLOSED", "MIXED")),
            ("LIGHTS", ("BRIGHT", "DIM", "OFF", "MIXED")),
        ):
            with self.subTest(control=control):
                symbols = [
                    self._render_status(control, state, "→ NEXT")
                    .crop((23, 22, 57, 43))
                    .tobytes()
                    for state in states
                ]
                self.assertEqual(len(symbols), len(set(symbols)))

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
