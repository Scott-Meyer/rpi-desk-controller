"""Native macOS audio device discovery and switching via CoreAudio."""

import ctypes
import logging
import sys
import time
from ctypes import POINTER, byref, c_bool, c_char_p, c_int32, c_long, c_uint32, c_void_p
from typing import List, Optional

from desk_controller.core.models import AudioDevice, AudioState
from desk_controller.desktop_agent.audio.base import BaseAudioDriver

logger = logging.getLogger(__name__)

_CORE_AUDIO_PATH = "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
_CORE_FOUNDATION_PATH = (
    "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
)
_SYSTEM_AUDIO_OBJECT = 1
_MAIN_ELEMENT = 0
_UTF8_ENCODING = 0x08000100


def _fourcc(value: str) -> int:
    """Convert a four-character CoreAudio constant to its UInt32 value."""
    if len(value) != 4:
        raise ValueError(f"CoreAudio constants must contain four characters: {value!r}")
    return int.from_bytes(value.encode("ascii"), "big")


class _AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("selector", c_uint32),
        ("scope", c_uint32),
        ("element", c_uint32),
    ]


class CoreAudioError(OSError):
    """Raised when a CoreAudio operation returns a non-zero OSStatus."""

    def __init__(self, operation: str, status: int):
        unsigned_status = status & 0xFFFFFFFF
        raw_status = unsigned_status.to_bytes(4, "big")
        readable_status = (
            raw_status.decode("ascii")
            if all(32 <= byte < 127 for byte in raw_status)
            else str(status)
        )
        super().__init__(f"{operation} failed with OSStatus {readable_status}")
        self.status = status


class CoreAudioBackend:
    """Small ctypes binding for the CoreAudio calls used by the agent."""

    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("CoreAudio is only available on macOS")

        self._core_audio = ctypes.CDLL(_CORE_AUDIO_PATH)
        self._core_foundation = ctypes.CDLL(_CORE_FOUNDATION_PATH)
        self._configure_functions()

    def _configure_functions(self) -> None:
        address_pointer = POINTER(_AudioObjectPropertyAddress)

        self._core_audio.AudioObjectGetPropertyDataSize.argtypes = [
            c_uint32,
            address_pointer,
            c_uint32,
            c_void_p,
            POINTER(c_uint32),
        ]
        self._core_audio.AudioObjectGetPropertyDataSize.restype = c_int32

        self._core_audio.AudioObjectGetPropertyData.argtypes = [
            c_uint32,
            address_pointer,
            c_uint32,
            c_void_p,
            POINTER(c_uint32),
            c_void_p,
        ]
        self._core_audio.AudioObjectGetPropertyData.restype = c_int32

        self._core_audio.AudioObjectSetPropertyData.argtypes = [
            c_uint32,
            address_pointer,
            c_uint32,
            c_void_p,
            c_uint32,
            c_void_p,
        ]
        self._core_audio.AudioObjectSetPropertyData.restype = c_int32

        self._core_foundation.CFStringGetLength.argtypes = [c_void_p]
        self._core_foundation.CFStringGetLength.restype = c_long
        self._core_foundation.CFStringGetMaximumSizeForEncoding.argtypes = [
            c_long,
            c_uint32,
        ]
        self._core_foundation.CFStringGetMaximumSizeForEncoding.restype = c_long
        self._core_foundation.CFStringGetCString.argtypes = [
            c_void_p,
            c_char_p,
            c_long,
            c_uint32,
        ]
        self._core_foundation.CFStringGetCString.restype = c_bool
        self._core_foundation.CFRelease.argtypes = [c_void_p]
        self._core_foundation.CFRelease.restype = None

    @staticmethod
    def _address(
        selector: str,
        scope: str = "glob",
    ) -> _AudioObjectPropertyAddress:
        return _AudioObjectPropertyAddress(
            _fourcc(selector),
            _fourcc(scope),
            _MAIN_ELEMENT,
        )

    @staticmethod
    def _check_status(operation: str, status: int) -> None:
        if status != 0:
            raise CoreAudioError(operation, status)

    def _get_property_size(
        self,
        object_id: int,
        selector: str,
        scope: str = "glob",
    ) -> int:
        address = self._address(selector, scope)
        size = c_uint32()
        status = self._core_audio.AudioObjectGetPropertyDataSize(
            object_id,
            byref(address),
            0,
            None,
            byref(size),
        )
        self._check_status(f"reading {selector!r} size", status)
        return size.value

    def _get_uint32(
        self,
        object_id: int,
        selector: str,
        scope: str = "glob",
    ) -> int:
        address = self._address(selector, scope)
        value = c_uint32()
        size = c_uint32(ctypes.sizeof(value))
        status = self._core_audio.AudioObjectGetPropertyData(
            object_id,
            byref(address),
            0,
            None,
            byref(size),
            byref(value),
        )
        self._check_status(f"reading {selector!r}", status)
        return value.value

    def _set_uint32(
        self,
        object_id: int,
        selector: str,
        value: int,
        scope: str = "glob",
    ) -> None:
        address = self._address(selector, scope)
        property_value = c_uint32(value)
        status = self._core_audio.AudioObjectSetPropertyData(
            object_id,
            byref(address),
            0,
            None,
            ctypes.sizeof(property_value),
            byref(property_value),
        )
        self._check_status(f"setting {selector!r}", status)

    def _get_cfstring(
        self,
        object_id: int,
        selector: str,
        scope: str = "glob",
    ) -> str:
        address = self._address(selector, scope)
        value = c_void_p()
        size = c_uint32(ctypes.sizeof(value))
        status = self._core_audio.AudioObjectGetPropertyData(
            object_id,
            byref(address),
            0,
            None,
            byref(size),
            byref(value),
        )
        self._check_status(f"reading {selector!r}", status)
        if not value.value:
            raise CoreAudioError(f"reading {selector!r}", -1)

        try:
            length = self._core_foundation.CFStringGetLength(value)
            buffer_size = (
                self._core_foundation.CFStringGetMaximumSizeForEncoding(
                    length,
                    _UTF8_ENCODING,
                )
                + 1
            )
            buffer = ctypes.create_string_buffer(buffer_size)
            converted = self._core_foundation.CFStringGetCString(
                value,
                buffer,
                buffer_size,
                _UTF8_ENCODING,
            )
            if not converted:
                raise UnicodeError(f"Could not decode CoreAudio property {selector!r}")
            return buffer.value.decode("utf-8")
        finally:
            self._core_foundation.CFRelease(value)

    def _device_ids(self) -> List[int]:
        size = self._get_property_size(_SYSTEM_AUDIO_OBJECT, "dev#")
        if size == 0:
            return []

        count = size // ctypes.sizeof(c_uint32)
        values = (c_uint32 * count)()
        address = self._address("dev#")
        data_size = c_uint32(size)
        status = self._core_audio.AudioObjectGetPropertyData(
            _SYSTEM_AUDIO_OBJECT,
            byref(address),
            0,
            None,
            byref(data_size),
            values,
        )
        self._check_status("listing audio devices", status)
        return list(values)

    def _has_output_streams(self, object_id: int) -> bool:
        try:
            return self._get_property_size(object_id, "stm#", scope="outp") > 0
        except CoreAudioError:
            return False

    def get_audio_state(self) -> AudioState:
        default_object_id = self._get_uint32(_SYSTEM_AUDIO_OBJECT, "dOut")
        devices: List[AudioDevice] = []
        active_name: Optional[str] = None

        for object_id in self._device_ids():
            if not self._has_output_streams(object_id):
                continue
            try:
                device_id = self._get_cfstring(object_id, "uid ")
                device_name = self._get_cfstring(object_id, "lnam")
            except (CoreAudioError, UnicodeError) as exc:
                logger.warning(
                    "Skipping unreadable CoreAudio device %s: %s", object_id, exc
                )
                continue

            is_default = object_id == default_object_id
            devices.append(
                AudioDevice(
                    id=device_id,
                    name=device_name,
                    is_default=is_default,
                )
            )
            if is_default:
                active_name = device_name

        return AudioState(
            active_device=active_name,
            available_devices=devices,
            is_muted=self.get_muted(),
        )

    def get_muted(self) -> bool:
        default_object_id = self._get_uint32(_SYSTEM_AUDIO_OBJECT, "dOut")
        return bool(
            self._get_uint32(
                default_object_id,
                "mute",
                scope="outp",
            )
        )

    def set_muted(self, muted: bool) -> bool:
        default_object_id = self._get_uint32(_SYSTEM_AUDIO_OBJECT, "dOut")
        self._set_uint32(
            default_object_id,
            "mute",
            int(muted),
            scope="outp",
        )
        return self.get_muted() == muted

    def set_output_device(self, device_name_or_id: str) -> bool:
        target = device_name_or_id.strip()
        if not target:
            return False

        candidates = []
        for object_id in self._device_ids():
            if not self._has_output_streams(object_id):
                continue
            try:
                device_id = self._get_cfstring(object_id, "uid ")
                device_name = self._get_cfstring(object_id, "lnam")
            except (CoreAudioError, UnicodeError):
                continue
            candidates.append((object_id, device_id, device_name))

        exact_matches = [
            candidate
            for candidate in candidates
            if target.casefold()
            in {
                candidate[1].casefold(),
                candidate[2].casefold(),
            }
        ]
        if len(exact_matches) != 1:
            logger.error(
                "Could not uniquely identify macOS audio output %r",
                device_name_or_id,
            )
            return False

        target_object_id, _, target_name = exact_matches[0]
        self._set_uint32(_SYSTEM_AUDIO_OBJECT, "dOut", target_object_id)

        try:
            self._set_uint32(_SYSTEM_AUDIO_OBJECT, "sOut", target_object_id)
        except CoreAudioError as exc:
            logger.warning(
                "Changed the default output to %s, but could not change system sounds: %s",
                target_name,
                exc,
            )

        for _ in range(10):
            if self._get_uint32(_SYSTEM_AUDIO_OBJECT, "dOut") == target_object_id:
                return True
            time.sleep(0.05)

        logger.error("CoreAudio did not confirm default output %s", target_name)
        return False


class MacOSAudioDriver(BaseAudioDriver):
    """Manage macOS output devices with the system CoreAudio framework."""

    def __init__(self, backend=None):
        self._backend = backend
        if self._backend is None:
            try:
                self._backend = CoreAudioBackend()
            except Exception as exc:
                logger.error("Could not initialize CoreAudio: %s", exc)

    def get_audio_state(self) -> AudioState:
        if self._backend is None:
            return AudioState(active_device=None, available_devices=[])
        try:
            return self._backend.get_audio_state()
        except Exception as exc:
            logger.error("Error querying macOS audio devices: %s", exc)
            return AudioState(active_device=None, available_devices=[])

    def set_output_device(self, device_name_or_id: str) -> bool:
        if self._backend is None:
            return False
        try:
            logger.info("Setting macOS audio output device to: %s", device_name_or_id)
            return self._backend.set_output_device(device_name_or_id)
        except Exception as exc:
            logger.error("Error setting macOS audio device: %s", exc)
            return False

    def set_muted(self, muted: bool) -> bool:
        if self._backend is None:
            return False
        try:
            logger.info("Setting macOS audio mute to: %s", muted)
            return self._backend.set_muted(muted)
        except Exception as exc:
            logger.error("Error setting macOS audio mute: %s", exc)
            return False
