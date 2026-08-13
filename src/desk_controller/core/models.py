"""
Core shared data models and schemas for Pi Controller & Desktop Agents.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class AudioDevice(BaseModel):
    id: str
    name: str
    is_default: bool = False
    is_output: bool = True


class AudioState(BaseModel):
    active_device: Optional[str] = None
    available_devices: List[AudioDevice] = []
    volume: Optional[int] = None
    is_muted: bool = False


class DeviceStatus(BaseModel):
    device_id: str
    hostname: str
    os_type: str
    lan_ip: Optional[str] = None
    app_version: str


class TelemetryMetrics(BaseModel):
    device_id: str
    hostname: str
    os_type: str  # "windows", "darwin", "linux"
    cpu_percent: float
    memory_percent: float
    lan_ip: Optional[str] = None
    gpu_temp: Optional[float] = None
    active_audio: Optional[str] = None
    audio_state: Optional[AudioState] = None
    custom_metrics: Optional[Dict[str, Any]] = None
