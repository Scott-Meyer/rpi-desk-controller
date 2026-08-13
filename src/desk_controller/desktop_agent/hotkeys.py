"""Cross-platform global shortcut registration for coordinated KVM requests."""

import ctypes
import logging
import platform
import threading
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable, Dict, FrozenSet

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class HotkeyBinding:
    modifiers: FrozenSet[str]
    key: str
    mac_key_code: int
    windows_virtual_key: int


_MODIFIER_ALIASES: Dict[str, str] = {
    "ctrl": "control",
    "control": "control",
    "alt": "option",
    "opt": "option",
    "option": "option",
    "cmd": "command",
    "command": "command",
    "win": "command",
    "super": "command",
    "shift": "shift",
}

_MAC_KEY_CODES = {
    **dict(
        zip(
            "abcdefghijklmnopqrstuvwxyz",
            [
                0,
                11,
                8,
                2,
                14,
                3,
                5,
                4,
                34,
                38,
                40,
                37,
                46,
                45,
                31,
                35,
                12,
                15,
                1,
                17,
                32,
                9,
                13,
                7,
                16,
                6,
            ],
        )
    ),
    **dict(zip("1234567890", [18, 19, 20, 21, 23, 22, 26, 28, 25, 29])),
    **{
        f"f{index}": key_code
        for index, key_code in enumerate(
            [122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111],
            start=1,
        )
    },
}


def parse_hotkey(value: str) -> HotkeyBinding:
    """Parse a human-readable shortcut without installing any OS hook."""
    parts = [
        part.strip().lower()
        for part in value.replace("-", "+").split("+")
        if part.strip()
    ]
    if len(parts) < 2:
        raise ValueError("global shortcut requires a modifier and one key")

    modifiers = set()
    key = None
    for part in parts:
        modifier = _MODIFIER_ALIASES.get(part)
        if modifier:
            modifiers.add(modifier)
            continue
        if key is not None:
            raise ValueError("global shortcut may contain only one non-modifier key")
        key = part

    if not modifiers or key is None:
        raise ValueError("global shortcut requires a modifier and one key")
    if key not in _MAC_KEY_CODES:
        raise ValueError("shortcut key must be A-Z, 0-9, or F1-F12")

    if len(key) == 1 and key.isalpha():
        windows_key = ord(key.upper())
    elif len(key) == 1 and key.isdigit():
        windows_key = ord(key)
    else:
        windows_key = 0x70 + int(key[1:]) - 1

    return HotkeyBinding(
        modifiers=frozenset(modifiers),
        key=key,
        mac_key_code=_MAC_KEY_CODES[key],
        windows_virtual_key=windows_key,
    )


def _four_char_code(value: str) -> int:
    return int.from_bytes(value.encode("mac_roman"), "big")


class MacOSGlobalHotkey:
    """Register one Carbon hotkey without Accessibility permission."""

    CMD_KEY = 1 << 8
    SHIFT_KEY = 1 << 9
    OPTION_KEY = 1 << 11
    CONTROL_KEY = 1 << 12
    EVENT_CLASS_KEYBOARD = _four_char_code("keyb")
    EVENT_HOTKEY_PRESSED = 5
    EVENT_PARAM_DIRECT_OBJECT = _four_char_code("----")
    TYPE_EVENT_HOTKEY_ID = _four_char_code("hkid")
    SIGNATURE = _four_char_code("DSKA")

    class EventTypeSpec(ctypes.Structure):
        _fields_ = [
            ("event_class", ctypes.c_uint32),
            ("event_kind", ctypes.c_uint32),
        ]

    class EventHotKeyID(ctypes.Structure):
        _fields_ = [
            ("signature", ctypes.c_uint32),
            ("identifier", ctypes.c_uint32),
        ]

    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self.carbon = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Carbon.framework/Frameworks/"
            "HIToolbox.framework/HIToolbox"
        )
        self._handler_ref = ctypes.c_void_p()
        self._hotkey_ref = ctypes.c_void_p()
        self._callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int32,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )
        self._callback_ref = self._callback_type(self._handle_event)
        self._configure_functions()
        event_type = self.EventTypeSpec(
            self.EVENT_CLASS_KEYBOARD,
            self.EVENT_HOTKEY_PRESSED,
        )
        result = self.carbon.InstallEventHandler(
            self.carbon.GetApplicationEventTarget(),
            self._callback_ref,
            1,
            ctypes.byref(event_type),
            None,
            ctypes.byref(self._handler_ref),
        )
        if result != 0:
            raise OSError(result, "Could not install the macOS hotkey handler")

    def _configure_functions(self) -> None:
        self.carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        self.carbon.InstallEventHandler.argtypes = [
            ctypes.c_void_p,
            self._callback_type,
            ctypes.c_uint32,
            ctypes.POINTER(self.EventTypeSpec),
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.carbon.GetEventParameter.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        self.carbon.RegisterEventHotKey.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint32,
            self.EventHotKeyID,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        self.carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        self.carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]

    def _handle_event(self, _next_handler, event, _user_data) -> int:
        hotkey_id = self.EventHotKeyID()
        result = self.carbon.GetEventParameter(
            event,
            self.EVENT_PARAM_DIRECT_OBJECT,
            self.TYPE_EVENT_HOTKEY_ID,
            None,
            ctypes.sizeof(hotkey_id),
            None,
            ctypes.byref(hotkey_id),
        )
        if (
            result == 0
            and hotkey_id.signature == self.SIGNATURE
            and hotkey_id.identifier == 1
        ):
            try:
                self.callback()
            except Exception:
                logger.exception("Global KVM hotkey callback failed")
        return 0

    def register(self, binding: HotkeyBinding) -> None:
        self.unregister()
        modifiers = 0
        for modifier, value in {
            "command": self.CMD_KEY,
            "shift": self.SHIFT_KEY,
            "option": self.OPTION_KEY,
            "control": self.CONTROL_KEY,
        }.items():
            if modifier in binding.modifiers:
                modifiers |= value
        hotkey_id = self.EventHotKeyID(self.SIGNATURE, 1)
        target = self.carbon.GetApplicationEventTarget()
        result = self.carbon.RegisterEventHotKey(
            binding.mac_key_code,
            modifiers,
            hotkey_id,
            target,
            0,
            ctypes.byref(self._hotkey_ref),
        )
        if result != 0:
            raise OSError(result, "The macOS global shortcut is unavailable")

    def unregister(self) -> None:
        if self._hotkey_ref.value:
            self.carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = ctypes.c_void_p()

    def stop(self) -> None:
        self.unregister()
        if self._handler_ref.value:
            self.carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = ctypes.c_void_p()


class WindowsGlobalHotkey:
    """Register one Windows hotkey on a dedicated message-loop thread."""

    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_SHIFT = 0x0004
    MOD_WIN = 0x0008
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012

    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self._binding = None
        self._thread = None
        self._thread_id = None
        self._started = threading.Event()
        self._error = None

    def register(self, binding: HotkeyBinding) -> None:
        self.stop()
        self._binding = binding
        self._started.clear()
        self._error = None
        self._thread = threading.Thread(
            target=self._run,
            name="desk-agent-hotkey",
            daemon=True,
        )
        self._thread.start()
        self._started.wait(3)
        if self._error is not None:
            raise self._error
        if not self._started.is_set():
            raise OSError("Windows global shortcut registration timed out")

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = kernel32.GetCurrentThreadId()
        modifiers = 0
        for modifier, value in {
            "option": self.MOD_ALT,
            "control": self.MOD_CONTROL,
            "shift": self.MOD_SHIFT,
            "command": self.MOD_WIN,
        }.items():
            if modifier in self._binding.modifiers:
                modifiers |= value
        if not user32.RegisterHotKey(
            None,
            1,
            modifiers,
            self._binding.windows_virtual_key,
        ):
            self._error = OSError("The Windows global shortcut is unavailable")
            self._started.set()
            return
        self._started.set()
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == self.WM_HOTKEY and message.wParam == 1:
                try:
                    self.callback()
                except Exception:
                    logger.exception("Global KVM hotkey callback failed")
        user32.UnregisterHotKey(None, 1)

    def unregister(self) -> None:
        self.stop()

    def stop(self) -> None:
        if self._thread_id is not None:
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id,
                self.WM_QUIT,
                0,
                0,
            )
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = None


class GlobalHotkeyManager:
    """Select the native backend and manage one current shortcut."""

    def __init__(
        self,
        callback: Callable[[], None],
        system: str = "",
    ):
        platform_name = (system or platform.system()).lower()
        if platform_name == "darwin":
            self.backend = MacOSGlobalHotkey(callback)
        elif platform_name == "windows":
            self.backend = WindowsGlobalHotkey(callback)
        else:
            self.backend = None
        self.current_shortcut = ""

    def configure(self, enabled: bool, shortcut: str) -> bool:
        if self.backend is None:
            return not enabled
        self.backend.unregister()
        self.current_shortcut = ""
        if not enabled:
            return True
        binding = parse_hotkey(shortcut)
        self.backend.register(binding)
        self.current_shortcut = shortcut
        logger.info("Global KVM shortcut registered: %s", shortcut)
        return True

    def stop(self) -> None:
        if self.backend is not None:
            self.backend.stop()
        self.current_shortcut = ""
