"""System-wide media metadata and controls for desktop workstation buttons."""

import ctypes
import json
import logging
import subprocess
import sys
import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MEDIA_REMOTE_PATH = "/System/Library/PrivateFrameworks/MediaRemote.framework"
_TOGGLE_PLAY_PAUSE_COMMAND = 2
_TITLE_KEY = "kMRMediaRemoteNowPlayingInfoTitle"
_ARTIST_KEY = "kMRMediaRemoteNowPlayingInfoArtist"
_PLAYBACK_RATE_KEY = "kMRMediaRemoteNowPlayingInfoPlaybackRate"
_NOW_PLAYING_JXA = (
    'ObjC.import("Foundation");'
    "const framework=$.NSBundle.bundleWithPath("
    '"/System/Library/PrivateFrameworks/MediaRemote.framework/");'
    "framework.load;"
    'const request=$.NSClassFromString("MRNowPlayingRequest");'
    "const item=request.localNowPlayingItem;"
    "if(!item) JSON.stringify(null);"
    "else {"
    "const info=item.nowPlayingInfo;"
    "const read=(key)=>{"
    "const value=info.valueForKey(key);"
    'return value ? ObjC.unwrap(value) : "";'
    "};"
    "JSON.stringify({"
    'title:read("kMRMediaRemoteNowPlayingInfoTitle"),'
    'artist:read("kMRMediaRemoteNowPlayingInfoArtist"),'
    "playing:Boolean(request.localIsPlaying)"
    "});"
    "}"
)


@dataclass(frozen=True)
class NowPlayingState:
    """Current system media presentation and playback state."""

    available: bool = False
    title: str = ""
    artist: str = ""
    is_playing: bool = False


class MacOSMediaDriver:
    """Read and control macOS's system-wide Now Playing session."""

    def __init__(self):
        if sys.platform != "darwin":
            raise OSError("MediaRemote is only available on macOS")

        import objc
        from Foundation import NSBundle

        dispatch = ctypes.CDLL(None)
        dispatch.dispatch_get_global_queue.argtypes = [
            ctypes.c_long,
            ctypes.c_ulong,
        ]
        dispatch.dispatch_get_global_queue.restype = ctypes.c_void_p
        queue_pointer = dispatch.dispatch_get_global_queue(0, 0)
        self._queue = objc.objc_object(c_void_p=queue_pointer)

        bundle = NSBundle.bundleWithPath_(_MEDIA_REMOTE_PATH)
        functions: Dict[str, Any] = {}
        callback_metadata = {
            "arguments": {
                1: {
                    "callable": {
                        "retval": {"type": b"v"},
                        "arguments": {
                            0: {"type": b"^v"},
                            1: {"type": b"@"},
                        },
                    }
                }
            }
        }
        objc.loadBundleFunctions(
            bundle,
            functions,
            [
                (
                    "MRMediaRemoteGetNowPlayingInfo",
                    b"v@@?",
                    "",
                    callback_metadata,
                ),
                ("MRMediaRemoteSendCommand", b"Zq@"),
            ],
            skip_undefined=False,
        )
        self._get_now_playing_info = functions["MRMediaRemoteGetNowPlayingInfo"]
        self._send_command = functions["MRMediaRemoteSendCommand"]

    @staticmethod
    def _text(info: Dict[str, Any], key: str) -> str:
        value = info.get(key)
        return str(value).strip() if value is not None else ""

    def get_now_playing(self, timeout: float = 1.0) -> NowPlayingState:
        """Return the current Now Playing metadata, waiting briefly for MediaRemote."""
        system_state = self._get_system_now_playing()
        if system_state is not None:
            return system_state

        # Direct framework access still works on macOS releases before 15.4.
        # Newer releases reject unentitled clients, so this is only a fallback
        # when the system osascript bridge itself is unavailable.
        completed = threading.Event()
        result: Dict[str, Any] = {}

        def receive_info(info):
            try:
                result.update(dict(info or {}))
            finally:
                completed.set()

        try:
            self._get_now_playing_info(self._queue, receive_info)
        except Exception:
            logger.exception("Could not request macOS Now Playing metadata")
            return NowPlayingState()

        if not completed.wait(timeout):
            logger.warning("Timed out reading macOS Now Playing metadata")
            return NowPlayingState()

        title = self._text(result, _TITLE_KEY)
        artist = self._text(result, _ARTIST_KEY)
        try:
            playback_rate = float(result.get(_PLAYBACK_RATE_KEY, 0) or 0)
        except (TypeError, ValueError):
            playback_rate = 0
        return NowPlayingState(
            available=bool(result),
            title=title,
            artist=artist,
            is_playing=playback_rate > 0,
        )

    @staticmethod
    def _get_system_now_playing() -> Optional[NowPlayingState]:
        """Query the OS-wide active session through MRNowPlayingRequest."""
        try:
            result = subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-l",
                    "JavaScript",
                    "-e",
                    _NOW_PLAYING_JXA,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            logger.debug(
                "Could not query the system Now Playing session",
                exc_info=True,
            )
            return None
        if result.returncode != 0:
            logger.debug(
                "System Now Playing request failed: %s",
                result.stderr.strip(),
            )
            return None
        try:
            payload = json.loads(result.stdout)
        except (TypeError, json.JSONDecodeError):
            logger.debug("System Now Playing request returned invalid JSON")
            return None
        if not isinstance(payload, dict):
            return NowPlayingState()
        return NowPlayingState(
            available=True,
            title=str(payload.get("title") or "").strip(),
            artist=str(payload.get("artist") or "").strip(),
            is_playing=bool(payload.get("playing")),
        )

    def toggle_play_pause(self) -> bool:
        """Send the native system play/pause command."""
        try:
            return bool(self._send_command(_TOGGLE_PLAY_PAUSE_COMMAND, None))
        except Exception:
            logger.exception("Could not toggle macOS media playback")
            return False
