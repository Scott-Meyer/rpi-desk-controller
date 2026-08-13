"""Windows audio driver backed by the native Core Audio APIs."""

import logging
from typing import Any, List, Optional

from desk_controller.core.models import AudioDevice, AudioState
from desk_controller.desktop_agent.audio.base import BaseAudioDriver

logger = logging.getLogger(__name__)


class PycawBackend:
    """Small lazy-loading adapter around pycaw's Windows Core Audio bindings."""

    def __init__(self):
        from pycaw.constants import DEVICE_STATE, EDataFlow, ERole
        from pycaw.utils import AudioUtilities

        self.audio_utilities = AudioUtilities
        self.device_state = DEVICE_STATE
        self.data_flow = EDataFlow
        self.roles = ERole

    def output_devices(self) -> List[Any]:
        return self.audio_utilities.GetAllDevices(
            self.data_flow.eRender.value,
            self.device_state.ACTIVE.value,
        )

    def default_output(self) -> Any:
        return self.audio_utilities.GetSpeakers()

    def set_default_output(self, device_id: str) -> None:
        self.audio_utilities.SetDefaultDevice(
            device_id,
            roles=[
                self.roles.eConsole,
                self.roles.eMultimedia,
                self.roles.eCommunications,
            ],
        )

    def set_muted(self, muted: bool) -> None:
        default = self.default_output()
        if default is None:
            raise RuntimeError("Windows has no active default output device")
        default.EndpointVolume.SetMute(bool(muted), None)


class WindowsAudioDriver(BaseAudioDriver):
    """Manage active Windows playback devices through Windows Core Audio."""

    def __init__(self, backend: Optional[PycawBackend] = None):
        super().__init__()
        self.backend = backend or PycawBackend()

    def get_audio_state(self) -> AudioState:
        devices: List[AudioDevice] = []
        active_name = None
        active_muted = False

        try:
            default = self.backend.default_output()
            default_id = str(getattr(default, "id", "")) if default else ""
            if default is not None:
                active_muted = bool(default.EndpointVolume.GetMute())

            for item in self.backend.output_devices():
                item_id = str(getattr(item, "id", "")).strip()
                name = str(getattr(item, "FriendlyName", "") or item_id).strip()
                if not item_id or not name:
                    continue
                is_default = item_id == default_id
                devices.append(
                    AudioDevice(
                        id=item_id,
                        name=name,
                        is_default=is_default,
                    )
                )
                if is_default:
                    active_name = name
        except Exception as exc:
            logger.error("Error querying Windows audio state: %s", exc)

        return AudioState(
            active_device=active_name,
            available_devices=devices,
            is_muted=active_muted,
        )

    def set_output_device(self, device_name_or_id: str) -> bool:
        target = device_name_or_id.strip()
        if not target:
            return False

        state = self.get_audio_state()
        target_folded = target.casefold()
        matched_id = next(
            (
                device.id
                for device in state.available_devices
                if target_folded == device.id.casefold()
                or target_folded == device.name.casefold()
            ),
            None,
        )
        if matched_id is None:
            matched_id = next(
                (
                    device.id
                    for device in state.available_devices
                    if target_folded in device.id.casefold()
                    or target_folded in device.name.casefold()
                ),
                None,
            )
        if matched_id is None:
            logger.error("Windows audio output was not found: %s", target)
            return False

        try:
            logger.info(
                "Setting Windows default playback device: %s",
                matched_id,
            )
            self.backend.set_default_output(matched_id)
            return True
        except Exception as exc:
            logger.error("Error setting Windows audio output device: %s", exc)
            return False

    def set_muted(self, muted: bool) -> bool:
        try:
            self.backend.set_muted(muted)
            return True
        except Exception as exc:
            logger.error("Error setting Windows audio mute: %s", exc)
            return False
