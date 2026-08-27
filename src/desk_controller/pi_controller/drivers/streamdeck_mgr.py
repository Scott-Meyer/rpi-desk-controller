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
