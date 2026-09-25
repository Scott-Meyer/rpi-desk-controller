"""
Elgato Stream Deck manager using python-elgato-streamdeck & PIL vector graphics renderer.
"""

import logging
import math
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

try:
    from StreamDeck.DeviceManager import DeviceManager
    from StreamDeck.ImageHelpers import PILHelper

    STREAMDECK_LIB_AVAILABLE = True
except ImportError:
    STREAMDECK_LIB_AVAILABLE = False
    logger.warning("StreamDeck SDK not installed. Running StreamDeck in mock mode.")


class StreamDeckManager:
    """Manages Stream Deck key rendering and button event handling."""

    CONTENT_INSET_RATIO = 0.15

    def __init__(self, key_callback=None, brightness: int = 85):
        self.deck = None
        self.key_callback = key_callback
        self.brightness = brightness

    def initialize(self) -> bool:
        if not STREAMDECK_LIB_AVAILABLE:
            logger.info("[Mock] Stream Deck initialized")
            return True

        streamdecks = DeviceManager().enumerate()
        if not streamdecks:
            logger.warning("No Stream Deck devices found attached to USB.")
            return False

        self.deck = streamdecks[0]
        self.deck.open()
        self.deck.reset()
        self.deck.set_brightness(self.brightness)

        # Register key callback
        self.deck.set_key_callback(self._on_key_change)
        logger.info(
            f"Connected to Stream Deck: {self.deck.deck_type()} ({self.deck.id()})"
        )
        return True

    def layout(self) -> tuple[int, int] | None:
        """Return the attached deck's (rows, columns), or None when undetected."""
        if self.deck is None:
            return None
        rows, columns = self.deck.key_layout()
        if rows <= 0 or columns <= 0 or rows * columns != self.deck.key_count():
            raise ValueError("Stream Deck reported an inconsistent physical layout")
        return rows, columns

    def key_image_size(self) -> tuple[int, int] | None:
        """Native key pixels, for consumers rendering the same artwork."""
        if self.deck is None:
            return None
        width, height = self.deck.key_image_format()["size"]
        return int(width), int(height)

    def _on_key_change(self, deck, key: int, state: bool):
        """Internal callback fired on key press / release."""
        if state and self.key_callback:
            self.key_callback(key)

    def _draw_sun_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple
    ):
        """Draws an antialiased sun with radiating rays."""
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        ray_len = r + 6
        ray_start = r + 2
        for angle_deg in range(0, 360, 45):
            rad = math.radians(angle_deg)
            x1 = cx + int(ray_start * math.cos(rad))
            y1 = cy + int(ray_start * math.sin(rad))
            x2 = cx + int(ray_len * math.cos(rad))
            y2 = cy + int(ray_len * math.sin(rad))
            draw.line([x1, y1, x2, y2], fill=color, width=2)

    def _draw_moon_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        r: int,
        color: tuple,
        bg_color: tuple,
    ):
        """Draws a crescent moon."""
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
        offset_r = int(r * 0.85)
        draw.ellipse(
            [
                cx - offset_r + 4,
                cy - offset_r - 4,
                cx + offset_r + 4,
                cy + offset_r - 4,
            ],
            fill=bg_color,
        )

    def _draw_snowflake_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple
    ):
        """Draw a six-point snowflake for the cold AC preset."""
        for angle in range(0, 360, 60):
            radians = math.radians(angle)
            dx, dy = math.cos(radians), math.sin(radians)
            draw.line(
                [cx, cy, cx + round(15 * dx), cy + round(15 * dy)],
                fill=color,
                width=3,
            )
            for side in (-1, 1):
                x, y = cx + 10 * dx, cy + 10 * dy
                branch = radians + side * math.pi * 3 / 4
                draw.line(
                    [
                        round(x),
                        round(y),
                        round(x + 5 * math.cos(branch)),
                        round(y + 5 * math.sin(branch)),
                    ],
                    fill=color,
                    width=2,
                )

    def _draw_bulb_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, color: tuple
    ):
        """Draws a glowing lightbulb icon."""
        draw.ellipse([cx - r, cy - r - 2, cx + r, cy + r - 6], fill=color)
        draw.rectangle([cx - r + 3, cy + r - 7, cx + r - 3, cy + r], fill=color)
        draw.rectangle(
            [cx - r + 4, cy + r + 1, cx + r - 4, cy + r + 4], fill=(120, 120, 130)
        )

    def _draw_speakers_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple
    ):
        """Draw a speaker cone with sound waves."""
        draw.rectangle([cx - 10, cy - 6, cx - 4, cy + 6], fill=color)
        draw.polygon(
            [(cx - 4, cy - 6), (cx + 4, cy - 12), (cx + 4, cy + 12), (cx - 4, cy + 6)],
            fill=color,
        )
        draw.arc(
            [cx + 2, cy - 8, cx + 10, cy + 8], start=-60, end=60, fill=color, width=2
        )
        draw.arc(
            [cx + 6, cy - 12, cx + 16, cy + 12], start=-60, end=60, fill=color, width=2
        )

    def _draw_mute_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple
    ):
        """Draw a speaker with a clear mute mark."""
        draw.rectangle([cx - 14, cy - 6, cx - 8, cy + 6], fill=color)
        draw.polygon(
            [
                (cx - 8, cy - 6),
                (cx, cy - 12),
                (cx, cy + 12),
                (cx - 8, cy + 6),
            ],
            fill=color,
        )
        draw.line([cx + 6, cy - 7, cx + 16, cy + 7], fill=color, width=3)
        draw.line([cx + 16, cy - 7, cx + 6, cy + 7], fill=color, width=3)

    def _draw_play_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        color: tuple,
    ):
        """Draw a media play triangle."""
        draw.polygon(
            [
                (cx - 8, cy - 12),
                (cx + 12, cy),
                (cx - 8, cy + 12),
            ],
            fill=color,
        )

    def _draw_pause_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        color: tuple,
    ):
        """Draw media pause bars."""
        draw.rounded_rectangle(
            [cx - 10, cy - 12, cx - 3, cy + 12],
            radius=2,
            fill=color,
        )
        draw.rounded_rectangle(
            [cx + 3, cy - 12, cx + 10, cy + 12],
            radius=2,
            fill=color,
        )

    def _draw_headphones_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        color: tuple,
        wireless: bool = False,
    ):
        """Draw over-ear headphones."""
        draw.arc(
            [cx - 12, cy - 14, cx + 12, cy + 4], start=180, end=360, fill=color, width=3
        )
        draw.rounded_rectangle([cx - 15, cy - 4, cx - 8, cy + 10], radius=3, fill=color)
        draw.rounded_rectangle([cx + 8, cy - 4, cx + 15, cy + 10], radius=3, fill=color)

        if wireless:
            draw.arc(
                [cx - 6, cy - 20, cx + 6, cy - 12],
                start=210,
                end=330,
                fill=color,
                width=2,
            )

    def _draw_earbuds_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple
    ):
        """Draw a microphone and earbuds icon."""
        draw.rounded_rectangle([cx - 6, cy - 14, cx + 6, cy + 2], radius=6, fill=color)
        draw.arc(
            [cx - 10, cy - 6, cx + 10, cy + 8], start=0, end=180, fill=color, width=2
        )
        draw.line([cx, cy + 8, cx, cy + 14], fill=color, width=2)
        draw.line([cx - 8, cy + 14, cx + 8, cy + 14], fill=color, width=2)

    def _draw_shades_icon(
        self, draw: ImageDraw.ImageDraw, cx: int, cy: int, color: tuple, mode: str
    ):
        """Draws window shades (close, privacy, extra_light)."""
        draw.rectangle([cx - 14, cy - 14, cx + 14, cy + 14], outline=color, width=2)

        if mode == "close":
            for y in range(cy - 10, cy + 12, 5):
                draw.line([cx - 12, y, cx + 12, y], fill=color, width=2)
        elif mode == "privacy":
            for y in range(cy - 10, cy + 1, 5):
                draw.line([cx - 12, y, cx + 12, y], fill=color, width=2)
            draw.ellipse([cx - 3, cy + 4, cx + 3, cy + 10], fill=color)
        elif mode == "extra_light":
            self._draw_sun_icon(draw, cx, cy, 6, color)

    def _draw_kvm_icon(
        self,
        draw: ImageDraw.ImageDraw,
        cx: int,
        cy: int,
        host_num: int,
        accent_color: tuple,
    ):
        """Draws a monitor/host switcher icon."""
        draw.rectangle(
            [cx - 16, cy - 14, cx + 16, cy + 6], outline=accent_color, width=2
        )
        draw.rectangle([cx - 3, cy + 7, cx + 3, cy + 11], fill=accent_color)
        draw.rectangle([cx - 8, cy + 12, cx + 8, cy + 14], fill=accent_color)
        draw.rectangle([cx - 13, cy - 11, cx + 13, cy + 3], fill=(20, 40, 70))
        draw.text((cx - 4, cy - 9), str(host_num), fill=accent_color)

    @staticmethod
    @lru_cache(maxsize=16)
    def _font(size: int, bold: bool = False):
        """Load a scalable UI font, falling back gracefully off the Pi."""
        filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        candidates = (
            Path("/usr/share/fonts/truetype/dejavu") / filename,
            Path("/usr/share/fonts/dejavu") / filename,
            Path("/System/Library/Fonts/SFCompact.ttf"),
        )
        for path in candidates:
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue

        try:
            return ImageFont.load_default(size=size)
        except TypeError:
            return ImageFont.load_default()

    @classmethod
    def _fit_font(
        cls,
        draw: ImageDraw.ImageDraw,
        text: str,
        max_width: int,
        preferred_size: int,
        minimum_size: int,
        *,
        bold: bool = False,
    ):
        """Return the largest font that keeps text inside the key."""
        for size in range(preferred_size, minimum_size - 1, -1):
            font = cls._font(size, bold)
            bounds = draw.textbbox((0, 0), text, font=font)
            if bounds[2] - bounds[0] <= max_width:
                return font
        return cls._font(minimum_size, bold)

    @staticmethod
    def _centered_text(
        draw: ImageDraw.ImageDraw,
        text: str,
        center_x: int,
        y: int,
        *,
        font,
        fill: tuple,
    ) -> None:
        """Draw text centered from measured glyph bounds instead of character count."""
        bounds = draw.textbbox((0, 0), text, font=font)
        text_width = bounds[2] - bounds[0]
        draw.text(
            (center_x - text_width / 2 - bounds[0], y),
            text,
            font=font,
            fill=fill,
        )

    @classmethod
    def _draw_datetime_display(
        cls,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        label: str,
        display_style: str,
        accent_color: tuple,
        text_color: tuple,
        safe_margin: int | None = None,
    ) -> None:
        """Render date and time with a clock-like typographic hierarchy."""
        primary, _, secondary = label.partition("\n")
        center_x = width // 2
        safe_margin = safe_margin or math.ceil(
            min(width, height) * cls.CONTENT_INSET_RATIO
        )
        safe_width = width - (safe_margin * 2)

        if display_style == "current_time":
            primary_font = cls._fit_font(
                draw,
                primary,
                safe_width,
                preferred_size=max(22, height // 3),
                minimum_size=12,
                bold=True,
            )
            primary_bounds = draw.textbbox((0, 0), primary, font=primary_font)
            primary_height = primary_bounds[3] - primary_bounds[1]
            if secondary:
                secondary = secondary.upper()
                secondary_font = cls._font(max(9, height // 8), bold=True)
                bounds = draw.textbbox((0, 0), secondary, font=secondary_font)
                pill_width = bounds[2] - bounds[0] + 12
                pill_height = bounds[3] - bounds[1] + 6
                gap = max(4, height // 14)
                content_top = max(
                    safe_margin,
                    (height - primary_height - gap - pill_height) // 2,
                )
                primary_y = content_top - primary_bounds[1]
                pill_top = content_top + primary_height + gap
                cls._centered_text(
                    draw,
                    primary,
                    center_x,
                    primary_y,
                    font=primary_font,
                    fill=text_color,
                )
                draw.rounded_rectangle(
                    [
                        center_x - pill_width // 2,
                        pill_top,
                        center_x + pill_width // 2,
                        pill_top + pill_height,
                    ],
                    radius=max(3, pill_height // 3),
                    fill=accent_color,
                )
                cls._centered_text(
                    draw,
                    secondary,
                    center_x,
                    pill_top - bounds[1] + 3,
                    font=secondary_font,
                    fill=(10, 10, 14),
                )
            else:
                cls._centered_text(
                    draw,
                    primary,
                    center_x,
                    (height - primary_height) // 2 - primary_bounds[1],
                    font=primary_font,
                    fill=text_color,
                )
            return

        eyebrow = primary.upper()
        eyebrow_font = cls._font(max(9, height // 8), bold=True)
        eyebrow_bounds = draw.textbbox((0, 0), eyebrow, font=eyebrow_font)
        cls._centered_text(
            draw,
            eyebrow,
            center_x,
            safe_margin - eyebrow_bounds[1],
            font=eyebrow_font,
            fill=accent_color,
        )
        date_font = cls._fit_font(
            draw,
            secondary,
            safe_width,
            preferred_size=max(18, height // 4),
            minimum_size=10,
            bold=True,
        )
        date_bounds = draw.textbbox((0, 0), secondary, font=date_font)
        date_height = date_bounds[3] - date_bounds[1]
        cls._centered_text(
            draw,
            secondary,
            center_x,
            height - safe_margin - date_height - date_bounds[1],
            font=date_font,
            fill=text_color,
        )

    @classmethod
    def _draw_button_label(
        cls,
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        label: str,
        text_color: tuple,
        safe_margin: int,
    ) -> None:
        """Bottom-align a label while keeping every glyph in the content-safe area."""
        lines = label.split("\n")
        safe_width = width - (safe_margin * 2)
        safe_height = height - (safe_margin * 2)
        longest_line = max(lines, key=len, default="")

        font = cls._font(6)
        line_gap = 1
        for size in range(10, 5, -1):
            candidate = cls._font(size)
            bounds = [draw.textbbox((0, 0), line, font=candidate) for line in lines]
            total_height = sum(bound[3] - bound[1] for bound in bounds)
            total_height += line_gap * max(0, len(lines) - 1)
            longest_bounds = draw.textbbox((0, 0), longest_line, font=candidate)
            if (
                longest_bounds[2] - longest_bounds[0] <= safe_width
                and total_height <= safe_height
            ):
                font = candidate
                break

        bounds = [draw.textbbox((0, 0), line, font=font) for line in lines]
        total_height = sum(bound[3] - bound[1] for bound in bounds)
        total_height += line_gap * max(0, len(lines) - 1)
        cursor_y = height - safe_margin - total_height
        for line, bound in zip(lines, bounds):
            cls._centered_text(
                draw,
                line,
                width // 2,
                cursor_y - bound[1],
                font=font,
                fill=text_color,
            )
            cursor_y += bound[3] - bound[1] + line_gap

    @staticmethod
    def _status_color(control: str, value: str) -> tuple[int, int, int]:
        """State hue belongs to what was observed, not the button's last press."""
        if control == "HOST":
            return {
                "PC 1": (65, 222, 242),
                "PC 2": (189, 149, 255),
                "PI": (91, 231, 175),
            }.get(value, (116, 134, 153))
        if control == "AC":
            return {"COLD": (117, 228, 255), "SLEEP": (187, 157, 255)}.get(
                value, (119, 135, 154)
            )
        if control == "SHADES":
            return {
                "OPEN": (255, 204, 103),
                "CLOSED": (143, 185, 251),
                "MIXED": (247, 173, 109),
                "MOVING": (255, 197, 84),
            }.get(value, (121, 140, 159))
        if control.startswith("BT") or value.startswith("LED"):
            return {"LED ON": (255, 213, 112), "LED OFF": (93, 124, 149)}.get(
                value, (114, 130, 149)
            )
        if control == "LIGHTS":
            return {
                "ON": (255, 210, 112),
                "BRIGHT": (255, 229, 131),
                "FULL": (255, 229, 131),
                "DIM": (162, 143, 111),
                "LOW": (162, 143, 111),
                "OFF": (98, 130, 155),
                "MIXED": (223, 169, 109),
            }.get(value, (116, 134, 153))
        return (144, 188, 226)

    @classmethod
    def _draw_choice_icon(
        cls,
        draw: ImageDraw.ImageDraw,
        control: str,
        choice: str,
        cx: int,
        cy: int,
        color: tuple[int, int, int],
        selected: bool,
        scale: float,
    ) -> None:
        """Native vector symbols; selected is larger, heavier, and brighter."""

        def px(value: float) -> int:
            return round(value * scale)

        thick = max(1, px(2.5 if selected else 1))
        if control == "AC" and choice == "COLD":
            radius = px(11 if selected else 8)
            for angle in range(0, 360, 60):
                rad = math.radians(angle)
                dx, dy = math.cos(rad), math.sin(rad)
                ex, ey = cx + round(radius * dx), cy + round(radius * dy)
                draw.line([cx, cy, ex, ey], fill=color, width=thick)
                for side in (-1, 1):
                    branch = rad + side * math.pi * 3 / 4
                    ox, oy = cx + radius * 0.7 * dx, cy + radius * 0.7 * dy
                    draw.line(
                        [
                            round(ox),
                            round(oy),
                            round(ox + px(4) * math.cos(branch)),
                            round(oy + px(4) * math.sin(branch)),
                        ],
                        fill=color,
                        width=thick,
                    )
            return
        if control == "AC" and choice == "SLEEP":
            # Headboard, mattress and pillow form a bed, not an ambiguous moon.
            w = px(21 if selected else 17)
            top = cy - px(7 if selected else 5)
            draw.rectangle(
                [cx - w // 2, top, cx - w // 2 + thick, cy + px(6)], fill=color
            )
            draw.rounded_rectangle(
                [cx - w // 2 + px(4), cy - px(1), cx + w // 2, cy + px(5)],
                radius=px(2),
                fill=color if selected else None,
                outline=color,
                width=thick,
            )
            draw.ellipse(
                [cx - w // 2 + px(4), top, cx - w // 2 + px(10), top + px(6)],
                fill=color if selected else None,
                outline=color,
                width=1,
            )
            draw.line(
                [cx - w // 2, cy + px(7), cx + w // 2, cy + px(7)],
                fill=color,
                width=thick,
            )
            return
        if control == "LIGHTS":
            radius = px(9 if selected else 7)
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=color if selected and choice == "ON" else None,
                outline=color,
                width=thick,
            )
            if choice == "OFF":
                draw.line(
                    [cx - radius, cy + radius, cx + radius, cy - radius],
                    fill=color,
                    width=thick,
                )
            return
        if control == "SHADES":
            w = px(22 if selected else 18)
            h = px(23 if selected else 18)
            x1, y1 = cx - w // 2, cy - h // 2
            x2, y2 = cx + w // 2, cy + h // 2
            draw.rectangle([x1, y1, x2, y2], outline=color, width=thick)
            count = 2 if choice == "OPEN" else 5
            for index in range(count):
                y = y1 + px(4) + index * (px(3) if choice == "CLOSED" else px(4))
                draw.line([x1 + px(2), y, x2 - px(2), y], fill=color, width=thick)
            if choice == "OPEN":
                draw.ellipse(
                    [cx - px(2), cy + px(3), cx + px(2), cy + px(7)], fill=color
                )

    @classmethod
    def _draw_host_monitor(
        cls,
        draw: ImageDraw.ImageDraw,
        observed: str,
        cx: int,
        cy: int,
        scale: float,
        pending: bool,
    ) -> None:
        """One monitor contains both hosts; only the observed numeral lights."""

        def px(value: float) -> int:
            return round(value * scale)

        color = cls._status_color("HOST", observed)
        outline = color if observed in {"PC 1", "PC 2", "PI"} else (98, 118, 136)
        draw.rounded_rectangle(
            [cx - px(24), cy - px(19), cx + px(24), cy + px(11)],
            radius=px(3),
            outline=outline,
            width=px(2),
            fill=tuple(10 + value // 7 for value in color),
        )
        for name, x in (("PC 1", cx - px(11)), ("PC 2", cx + px(11))):
            selected = observed == name
            digit = name[-1]
            font = cls._font(px(20 if selected else 12), bold=selected)
            bounds = draw.textbbox((0, 0), digit, font=font)
            ink = cls._status_color("HOST", name) if selected else (72, 90, 108)
            if selected and pending:
                ink = tuple(min(255, value + 14) for value in ink)
            cls._centered_text(
                draw,
                digit,
                x,
                cy - px(5) - (bounds[3] - bounds[1]) // 2 - bounds[1],
                font=font,
                fill=ink,
            )
        draw.line([cx, cy + px(12), cx, cy + px(17)], fill=outline, width=px(2))
        draw.line(
            [cx - px(8), cy + px(18), cx + px(8), cy + px(18)],
            fill=outline,
            width=px(2),
        )

    @classmethod
    def _draw_bulb_key(
        cls,
        draw: ImageDraw.ImageDraw,
        control: str,
        observed: str,
        cx: int,
        cy: int,
        scale: float,
        phase: str,
    ) -> None:
        """1 is half-filled, 2 filled, 3 filled with rays; LED controls glow."""

        def px(value: float) -> int:
            return round(value * scale)

        level = int(control[-1])
        lit = observed == "LED ON"
        unknown = observed not in {"LED ON", "LED OFF"}
        glow = {1: (174, 151, 101), 2: (224, 185, 99), 3: (255, 223, 125)}.get(
            level, (255, 223, 125)
        )
        outline = glow if lit else (119, 137, 155) if unknown else (72, 92, 110)
        if phase == "pending":
            outline = tuple(min(255, value + 19) for value in outline)
        bulb_y = cy - px(4)
        radius = px(11)
        globe = [cx - radius, bulb_y - radius, cx + radius, bulb_y + radius]
        if lit:
            draw.ellipse(
                [cx - px(16), bulb_y - px(16), cx + px(16), bulb_y + px(16)],
                fill=tuple(17 + component // 6 for component in glow),
            )
        if level >= 3:
            for angle in range(0, 360, 45):
                radians = math.radians(angle)
                if math.sin(radians) > 0.55:
                    continue
                draw.line(
                    [
                        cx + round(px(13) * math.cos(radians)),
                        bulb_y + round(px(13) * math.sin(radians)),
                        cx + round(px(18) * math.cos(radians)),
                        bulb_y + round(px(18) * math.sin(radians)),
                    ],
                    fill=outline,
                    width=px(2 if lit else 1),
                )
        unlit_fill = (17, 24, 33)
        if unknown:
            draw.ellipse(globe, fill=(25, 35, 46), outline=outline, width=px(2))
            font = cls._font(px(15), bold=True)
            bounds = draw.textbbox((0, 0), "?", font=font)
            cls._centered_text(
                draw,
                "?",
                cx,
                bulb_y - (bounds[3] - bounds[1]) // 2 - bounds[1],
                font=font,
                fill=(222, 232, 240),
            )
        elif level == 1:
            draw.ellipse(
                globe, fill=unlit_fill, outline=outline, width=px(3 if lit else 2)
            )
            # Half of the globe is filled even while its LED is off; then it
            # remains engraved but subdued rather than appearing selected.
            draw.pieslice(globe, 90, 270, fill=glow if lit else (42, 51, 57))
            draw.ellipse(globe, outline=outline, width=px(3 if lit else 2))
        else:
            draw.ellipse(
                globe,
                fill=glow if lit else (37, 45, 52),
                outline=outline,
                width=px(3 if lit else 2),
            )
        draw.rectangle(
            [cx - px(5), bulb_y + px(10), cx + px(5), bulb_y + px(15)],
            fill=outline if lit else (37, 48, 60),
            outline=outline,
            width=1,
        )
        font = cls._font(px(11), bold=True)
        bounds = draw.textbbox((0, 0), str(level), font=font)
        cls._centered_text(
            draw,
            str(level),
            cx,
            cy + px(16) - bounds[1],
            font=font,
            fill=(243, 246, 251) if lit else (133, 151, 169),
        )

    @classmethod
    def _draw_generic_keypad(
        cls,
        draw: ImageDraw.ImageDraw,
        label: str,
        observed: str,
        width: int,
        height: int,
        inset: int,
    ) -> None:
        """Keep custom HA button names and their independent LED readable."""
        scale = min(width, height) / 80

        def px(value: float) -> int:
            return round(value * scale)

        color = cls._status_color(label, observed)
        draw.rounded_rectangle(
            [
                inset + px(2),
                inset + px(7),
                width - inset - px(3),
                height - inset - px(5),
            ],
            radius=px(5),
            outline=color,
            width=px(2),
        )
        caption = " ".join(label.split()) or "KEY"
        available = width - inset * 2 - px(8)
        font = cls._fit_font(draw, caption, available, px(11), px(8), bold=True)
        while caption and draw.textbbox((0, 0), caption, font=font)[2] > available:
            caption = caption[:-1]
        bounds = draw.textbbox((0, 0), caption or "KEY", font=font)
        cls._centered_text(
            draw,
            caption or "KEY",
            width // 2,
            inset + px(13) - bounds[1],
            font=font,
            fill=(238, 244, 250),
        )
        cy = height - inset - px(16)
        cx = width // 2
        if observed == "LED ON":
            draw.ellipse(
                [cx - px(7), cy - px(7), cx + px(7), cy + px(7)], fill=(106, 78, 32)
            )
            draw.ellipse(
                [cx - px(5), cy - px(5), cx + px(5), cy + px(5)], fill=(255, 219, 119)
            )
        else:
            draw.ellipse(
                [cx - px(6), cy - px(6), cx + px(6), cy + px(6)],
                outline=(133, 159, 184) if observed == "LED OFF" else (187, 198, 210),
                width=px(2),
            )
            if observed != "LED OFF":
                font = cls._font(px(10), bold=True)
                bounds = draw.textbbox((0, 0), "?", font=font)
                cls._centered_text(
                    draw,
                    "?",
                    cx,
                    cy - (bounds[3] - bounds[1]) // 2 - bounds[1],
                    font=font,
                    fill=(231, 237, 245),
                )

    @classmethod
    def status_image(
        cls,
        control: str,
        observed: str | None,
        next_action: str | None,
        *,
        phase: str = "ready",
        target: str | None = None,
        size: tuple[int, int] = (80, 80),
    ) -> Image.Image:
        """The exact physical key artwork, also usable by the web editor."""
        control = " ".join(control.upper().split())
        observed = (observed or "?").upper().strip() or "?"
        numbered_keypad = control in {"BT1", "BT2", "BT3", "BT4"}
        generic_keypad = observed.startswith("LED") and not numbered_keypad
        if control not in {"HOST", "AC", "SHADES", "LIGHTS"} and not (
            numbered_keypad or generic_keypad
        ):
            raise ValueError(f"Unknown status control: {control}")
        if phase not in {"ready", "pending", "ack", "blocked", "error", "unknown"}:
            raise ValueError(f"Unknown status phase: {phase}")
        if phase == "unknown" and not generic_keypad and not numbered_keypad:
            observed = "?"
        elif observed == "?" and phase == "ready":
            phase = "unknown"
        width, height = size
        scale = min(width, height) / 80

        def px(value: float) -> int:
            return round(value * scale)

        bg = (8, 12, 20)
        img = Image.new("RGB", (width, height), bg)
        draw = ImageDraw.Draw(img)
        content = Image.new("RGB", (width, height), bg)
        canvas = ImageDraw.Draw(content)
        inset = math.ceil(min(width, height) * cls.CONTENT_INSET_RATIO)
        cx = width // 2
        observed_color = cls._status_color(control, observed)
        border = {
            "pending": (255, 196, 0),
            "ack": (102, 230, 151),
            "error": (255, 77, 88),
            "unknown": (88, 106, 122),
            "blocked": (148, 164, 178),
        }.get(phase, observed_color)
        if phase == "pending":
            cls._draw_dotted_border(draw, width, height, border)
        elif phase == "error":
            draw.rounded_rectangle(
                [px(2), px(2), width - px(3), height - px(3)],
                radius=px(7),
                outline=border,
                width=px(2),
            )
        if numbered_keypad:
            cls._draw_bulb_key(canvas, control, observed, cx, height // 2, scale, phase)
        elif generic_keypad:
            cls._draw_generic_keypad(canvas, control, observed, width, height, inset)
        elif control == "HOST":
            cls._draw_host_monitor(
                canvas, observed, cx, height // 2 - px(2), scale, phase == "pending"
            )
        else:
            choices = {
                "AC": ("COLD", "SLEEP"),
                "SHADES": ("OPEN", "CLOSED"),
                "LIGHTS": ("OFF", "ON"),
            }[control]
            heading = control
            font = cls._font(px(10 if control == "AC" else 9), bold=True)
            box = canvas.textbbox((0, 0), heading, font=font)
            cls._centered_text(
                canvas,
                heading,
                cx,
                inset + px(1) - box[1],
                font=font,
                fill=(199, 213, 229),
            )
            left_center, right_center = cx - px(14), cx + px(14)
            for choice, center in zip(choices, (left_center, right_center)):
                selected = observed == choice or (
                    control == "LIGHTS"
                    and choice == "ON"
                    and observed in {"BRIGHT", "FULL", "DIM", "LOW"}
                )
                hue = cls._status_color(
                    control, observed if selected and choice == "ON" else choice
                )
                icon_color = (
                    tuple(
                        min(255, component + (15 if phase == "pending" else 0))
                        for component in hue
                    )
                    if selected
                    else (69, 88, 105)
                )
                icon_y = height // 2 - px(1)
                if selected:
                    canvas.ellipse(
                        [
                            center - px(15),
                            icon_y - px(15),
                            center + px(15),
                            icon_y + px(15),
                        ],
                        fill=tuple(11 + component // 7 for component in icon_color),
                    )
                cls._draw_choice_icon(
                    canvas,
                    control,
                    choice,
                    center,
                    icon_y,
                    icon_color,
                    selected,
                    scale,
                )
        # No alternative is lit for PI, mixed/custom, moving, or unknown.
        # The tiny marker names these without inventing a selected source.
        if not (numbered_keypad or generic_keypad):
            extra = (
                "PI"
                if control == "HOST" and observed == "PI"
                else "?"
                if phase == "unknown"
                else "·"
                if observed in {"MIXED", "MOVING", "CUSTOM"}
                else observed
                if control != "HOST"
                and observed
                not in {
                    "COLD",
                    "SLEEP",
                    "OPEN",
                    "CLOSED",
                    "OFF",
                    "ON",
                    "BRIGHT",
                    "DIM",
                    "FULL",
                    "LOW",
                }
                and phase not in {"unknown", "blocked"}
                else ""
            )
            if extra:
                font = cls._fit_font(
                    canvas,
                    extra,
                    width - inset * 2 - px(4),
                    px(9 if extra == "PI" else 11),
                    px(8),
                    bold=True,
                )
                box = canvas.textbbox((0, 0), extra, font=font)
                cls._centered_text(
                    canvas,
                    extra,
                    cx,
                    height - inset - px(9) - box[1],
                    font=font,
                    fill=(106, 234, 179)
                    if extra == "PI"
                    else (255, 191, 91)
                    if extra == "·"
                    else (204, 220, 232),
                )
        if phase in {"ack", "error"}:
            mark = "✓" if phase == "ack" else "!"
            font = cls._font(px(12), bold=True)
            box = canvas.textbbox((0, 0), mark, font=font)
            canvas.text(
                (width - inset - px(2) - (box[2] - box[0]), inset + px(1) - box[1]),
                mark,
                fill=border,
                font=font,
            )
        safe_box = (inset, inset, width - inset, height - inset)
        img.paste(content.crop(safe_box), safe_box[:2])
        return img

    def render_status_key(
        self,
        key: int,
        control: str,
        observed: str | None,
        next_action: str | None,
        *,
        phase: str = "ready",
        target: str | None = None,
    ) -> None:
        """Send the shared status artwork to the attached Stream Deck."""
        if not STREAMDECK_LIB_AVAILABLE or not self.deck:
            logger.info(
                "[Mock] Stream Deck Key %s [%s] %s: %s (next %s)",
                key,
                phase.upper(),
                control,
                observed,
                target if phase == "pending" and target else next_action,
            )
            return
        image = self.status_image(
            control,
            observed,
            next_action,
            phase=phase,
            target=target,
            size=self.deck.key_image_format()["size"],
        )
        native_image = PILHelper.to_native_format(self.deck, image)
        with self.deck:
            self.deck.set_key_image(key, native_image)

    def render_scene_key(
        self,
        key: int,
        label: str,
        icon_type: str,
        is_active: bool = False,
        accent_color=(255, 200, 0),
        host_num: int = 1,
        is_available=None,
        is_pending: bool = False,
        has_error: bool = False,
        display_style: str = "button",
    ):
        """Renders key image with PIL vector graphics and dynamic glowing states."""
        if not STREAMDECK_LIB_AVAILABLE or not self.deck:
            status = "LIT" if is_active else "DARK"
            if is_available is False:
                status = "OFFLINE"
            if is_pending:
                status = "PENDING"
            logger.info(
                f"[Mock] Stream Deck Key {key} [{status}] -> '{label}' ({icon_type})"
            )
            return

        key_format = self.deck.key_image_format()
        width, height = key_format["size"]
        bg_color = (10, 10, 14)

        img = Image.new("RGB", (width, height), color=bg_color)
        draw = ImageDraw.Draw(img)
        content = Image.new("RGB", (width, height), color=bg_color)
        content_draw = ImageDraw.Draw(content)
        safe_margin = math.ceil(min(width, height) * self.CONTENT_INSET_RATIO)

        cx, cy = width // 2, 28

        if is_available is False:
            draw.rectangle(
                [2, 2, width - 3, height - 3],
                outline=(24, 24, 30),
                width=1,
            )
            icon_color = (55, 55, 65)
            text_color = (55, 55, 65)
        elif is_active:
            draw.rectangle([1, 1, width - 2, height - 2], outline=accent_color, width=4)
            draw.rectangle(
                [4, 4, width - 5, height - 5],
                outline=(
                    accent_color[0] // 3,
                    accent_color[1] // 3,
                    accent_color[2] // 3,
                ),
                width=1,
            )
            icon_color = accent_color
            text_color = (255, 255, 255)
        else:
            draw.rectangle([2, 2, width - 3, height - 3], outline=(40, 40, 50), width=1)
            icon_color = (130, 130, 145)
            text_color = (120, 120, 130)

        if has_error:
            draw.rectangle(
                [2, 2, width - 3, height - 3], outline=(255, 70, 70), width=3
            )

        if is_pending:
            self._draw_dotted_border(
                draw,
                width,
                height,
                color=(255, 196, 0),
            )

        if display_style in {"current_time", "current_date"}:
            self._draw_datetime_display(
                content_draw,
                width,
                height,
                label,
                display_style,
                accent_color,
                text_color,
                safe_margin,
            )
        else:
            # Draw vector icon by type
            if icon_type == "sun":
                self._draw_sun_icon(content_draw, cx, cy, 10, icon_color)
            elif icon_type == "moon":
                self._draw_moon_icon(content_draw, cx, cy, 11, icon_color, bg_color)
            elif icon_type == "snowflake":
                self._draw_snowflake_icon(content_draw, cx, cy, icon_color)
            elif icon_type == "bulb":
                self._draw_bulb_icon(content_draw, cx, cy, 10, icon_color)
            elif icon_type == "speakers":
                self._draw_speakers_icon(content_draw, cx, cy, icon_color)
            elif icon_type == "mute":
                self._draw_mute_icon(content_draw, cx, cy, icon_color)
            elif icon_type == "play":
                self._draw_play_icon(content_draw, cx, cy, icon_color)
            elif icon_type == "pause":
                self._draw_pause_icon(content_draw, cx, cy, icon_color)
            elif icon_type == "headphones":
                self._draw_headphones_icon(
                    content_draw, cx, cy, icon_color, wireless=False
                )
            elif icon_type == "wireless_headphones":
                self._draw_headphones_icon(
                    content_draw, cx, cy, icon_color, wireless=True
                )
            elif icon_type == "earbuds":
                self._draw_earbuds_icon(content_draw, cx, cy, icon_color)
            elif icon_type.startswith("shades_"):
                mode = icon_type.replace("shades_", "")
                self._draw_shades_icon(content_draw, cx, cy, icon_color, mode)
            elif icon_type == "kvm":
                self._draw_kvm_icon(content_draw, cx, cy, host_num, icon_color)

            self._draw_button_label(
                content_draw,
                width,
                height,
                label,
                text_color,
                safe_margin,
            )

        safe_box = (
            safe_margin,
            safe_margin,
            width - safe_margin,
            height - safe_margin,
        )
        img.paste(content.crop(safe_box), safe_box[:2])

        native_image = PILHelper.to_native_format(self.deck, img)
        with self.deck:
            self.deck.set_key_image(key, native_image)

    @staticmethod
    def _draw_dotted_border(
        draw: ImageDraw.ImageDraw,
        width: int,
        height: int,
        color: tuple,
    ) -> None:
        """Draw a high-contrast pending border without implying completion."""
        inset = 2
        dash = 4
        gap = 4
        line_width = 2
        for x in range(inset, width - inset, dash + gap):
            x_end = min(x + dash - 1, width - inset - 1)
            draw.line(
                [(x, inset), (x_end, inset)],
                fill=color,
                width=line_width,
            )
            draw.line(
                [(x, height - inset - 1), (x_end, height - inset - 1)],
                fill=color,
                width=line_width,
            )
        for y in range(inset, height - inset, dash + gap):
            y_end = min(y + dash - 1, height - inset - 1)
            draw.line(
                [(inset, y), (inset, y_end)],
                fill=color,
                width=line_width,
            )
            draw.line(
                [(width - inset - 1, y), (width - inset - 1, y_end)],
                fill=color,
                width=line_width,
            )

    def close(self):
        if self.deck:
            with self.deck:
                self.deck.reset()
                self.deck.close()
