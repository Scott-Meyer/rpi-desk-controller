"""
Abstract base class for OS-specific audio device management.
"""

from abc import ABC, abstractmethod

from desk_controller.core.models import AudioState


class BaseAudioDriver(ABC):
    """Abstract interface for querying and switching OS audio devices."""

    @abstractmethod
    def get_audio_state(self) -> AudioState:
        """Returns the current audio state (active device, list of devices, volume)."""
        pass

    @abstractmethod
    def set_output_device(self, device_name_or_id: str) -> bool:
        """Sets the active default audio output device."""
        pass

    @abstractmethod
    def set_muted(self, muted: bool) -> bool:
        """Set mute on the active default output device."""
        pass

    def toggle_mute(self) -> bool:
        """Toggle mute on the active default output device."""
        return self.set_muted(not self.get_audio_state().is_muted)
