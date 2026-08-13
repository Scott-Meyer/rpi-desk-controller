"""Local registry for workstation-owned Stream Deck buttons."""

import os
import subprocess
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Literal, Mapping

import psutil
from pydantic import Field, model_validator

from desk_controller.core.workstation_slots import WorkstationSlotPresentation
from desk_controller.desktop_agent.hotkeys import parse_hotkey
from desk_controller.desktop_agent.media import NowPlayingState

DESKTOP_ACTION_CATALOG = [
    {
        "type": "app",
        "label": "Open application",
        "fields": ["launch_target", "process_names"],
        "state": "application_running",
    },
    {
        "type": "open",
        "label": "Open file or URL",
        "fields": ["launch_target"],
        "state": "none",
    },
    {
        "type": "hotkey",
        "label": "Send keyboard shortcut",
        "fields": ["shortcut"],
        "state": "none",
    },
    {
        "type": "audio_mute",
        "label": "Mute / unmute",
        "fields": [],
        "state": "audio_muted",
    },
    {
        "type": "media_play_pause",
        "label": "Play / pause media",
        "fields": [],
        "state": "media_playing",
    },
]


class DesktopWorkstationButton(WorkstationSlotPresentation):
    """Local action details that are never advertised to the Pi."""

    action_type: Literal[
        "app",
        "open",
        "hotkey",
        "audio_mute",
        "media_play_pause",
    ] = "app"
    process_names: List[str] = Field(default_factory=list, max_length=32)
    launch_target: str = Field(default="", max_length=2048)
    shortcut: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action_type in {"app", "open"} and not self.launch_target.strip():
            raise ValueError(
                f"{self.action_type} workstation buttons require launch_target"
            )
        if self.action_type == "hotkey":
            parse_hotkey(self.shortcut)
        return self


class DesktopWorkstationButtonRegistry:
    """Observe and activate locally configured workstation buttons."""

    def __init__(
        self,
        slots: Dict[str, DesktopWorkstationButton],
        os_type: str,
    ):
        self._slots = dict(slots)
        self.os_type = os_type.lower()

    def configured_slots(self) -> List[Dict[str, Any]]:
        """Return editable local action details in stable slot order."""
        return [
            slot.model_dump(mode="json")
            for slot in sorted(
                self._slots.values(),
                key=lambda item: item.slot_id,
            )
        ]

    @classmethod
    def from_config(
        cls,
        config: Any,
        os_type: str,
    ) -> "DesktopWorkstationButtonRegistry":
        slots: Dict[str, DesktopWorkstationButton] = {}
        if isinstance(config, Mapping):
            for raw_slot_id, raw_slot in config.items():
                if not isinstance(raw_slot, Mapping):
                    continue
                values = dict(raw_slot)
                values["slot_id"] = str(raw_slot_id)
                slot = DesktopWorkstationButton.model_validate(values)
                slots[slot.slot_id] = slot
        return cls(slots, os_type)

    @staticmethod
    def _now_playing_label(media: NowPlayingState) -> str:
        if not media.available:
            return "NO MEDIA"
        name = media.title or media.artist or "NOW PLAYING"
        lines = textwrap.wrap(
            name,
            width=10,
            max_lines=2,
            placeholder="…",
            break_long_words=True,
            break_on_hyphens=True,
        )
        return "\n".join(lines)

    def manifest_slots(
        self,
        media: NowPlayingState = NowPlayingState(),
    ) -> List[WorkstationSlotPresentation]:
        presentations = []
        for slot in self._slots.values():
            label = slot.label
            icon = slot.icon
            if slot.action_type == "media_play_pause":
                label = self._now_playing_label(media)
                icon = "pause" if media.is_playing else "play"
            presentations.append(
                WorkstationSlotPresentation(
                    slot_id=slot.slot_id,
                    label=label,
                    icon=icon,
                    accent_color=slot.accent_color,
                )
            )
        return presentations

    @staticmethod
    def _running_process_names() -> set:
        running = set()
        for process in psutil.process_iter(["name", "exe"]):
            try:
                name = process.info.get("name")
                executable = process.info.get("exe")
                if name:
                    running.add(str(name).casefold())
                if executable:
                    running.add(Path(str(executable)).name.casefold())
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
        return running

    def active_state(
        self,
        audio_muted: bool = False,
        media: NowPlayingState = NowPlayingState(),
    ) -> Dict[str, bool]:
        running_names = (
            self._running_process_names()
            if any(slot.action_type == "app" for slot in self._slots.values())
            else set()
        )
        state = {}
        for slot_id, slot in self._slots.items():
            if slot.action_type == "audio_mute":
                state[slot_id] = audio_muted
                continue
            if slot.action_type == "media_play_pause":
                state[slot_id] = media.is_playing
                continue
            if slot.action_type != "app":
                state[slot_id] = False
                continue
            expected_names = {
                process_name.casefold()
                for process_name in slot.process_names
                if process_name.strip()
            }
            if not expected_names:
                expected_names.add(Path(slot.launch_target).name.casefold())
            state[slot_id] = bool(expected_names & running_names)
        return state

    def activate(
        self,
        slot_id: str,
        audio_driver=None,
        media_driver=None,
    ) -> bool:
        """Execute the locally configured action without accepting remote details."""
        slot = self._slots.get(slot_id)
        if slot is None:
            return False
        if slot.action_type == "audio_mute":
            return bool(audio_driver and audio_driver.toggle_mute())
        if slot.action_type == "media_play_pause":
            return bool(media_driver and media_driver.toggle_play_pause())
        if slot.action_type == "hotkey":
            return self._send_hotkey(slot.shortcut)
        if slot.action_type == "open":
            return self._open_target(slot.launch_target)

        try:
            if self.os_type == "darwin":
                result = subprocess.run(
                    ["open", "-a", slot.launch_target],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                return result.returncode == 0
            if self.os_type == "windows":
                os.startfile(slot.launch_target)  # type: ignore[attr-defined]
                return True
            result = subprocess.Popen([slot.launch_target])
            return result.poll() is None
        except (OSError, subprocess.SubprocessError):
            return False

    def _open_target(self, target: str) -> bool:
        try:
            if self.os_type == "darwin":
                result = subprocess.run(
                    ["open", target],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                return result.returncode == 0
            if self.os_type == "windows":
                os.startfile(target)  # type: ignore[attr-defined]
                return True
            result = subprocess.run(
                ["xdg-open", target],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _send_hotkey(self, shortcut: str) -> bool:
        binding = parse_hotkey(shortcut)
        try:
            if self.os_type == "darwin":
                modifiers = {
                    "control": "control down",
                    "option": "option down",
                    "shift": "shift down",
                    "command": "command down",
                }
                using = ", ".join(
                    modifiers[name]
                    for name in ("control", "option", "shift", "command")
                    if name in binding.modifiers
                )
                script = (
                    'tell application "System Events" to key code '
                    f"{binding.mac_key_code} using {{{using}}}"
                )
                result = subprocess.run(
                    ["osascript", "-e", script],
                    check=False,
                    capture_output=True,
                    timeout=10,
                )
                return result.returncode == 0
            if self.os_type == "windows":
                return self._send_windows_hotkey(binding)
            keys = [
                {
                    "control": "ctrl",
                    "option": "alt",
                    "shift": "shift",
                    "command": "super",
                }[name]
                for name in ("control", "option", "shift", "command")
                if name in binding.modifiers
            ]
            keys.append(binding.key)
            result = subprocess.run(
                ["xdotool", "key", "+".join(keys)],
                check=False,
                capture_output=True,
                timeout=10,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    @staticmethod
    def _send_windows_hotkey(binding) -> bool:
        import ctypes

        user32 = ctypes.windll.user32
        key_up = 0x0002
        modifier_keys = {
            "control": 0x11,
            "option": 0x12,
            "shift": 0x10,
            "command": 0x5B,
        }
        pressed = [
            modifier_keys[name]
            for name in ("control", "option", "shift", "command")
            if name in binding.modifiers
        ]
        try:
            for virtual_key in pressed:
                user32.keybd_event(virtual_key, 0, 0, 0)
            user32.keybd_event(binding.windows_virtual_key, 0, 0, 0)
            user32.keybd_event(binding.windows_virtual_key, 0, key_up, 0)
            for virtual_key in reversed(pressed):
                user32.keybd_event(virtual_key, 0, key_up, 0)
        except Exception:
            return False
        return True
