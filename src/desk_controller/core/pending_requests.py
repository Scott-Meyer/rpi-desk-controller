"""Bounded, thread-safe retry state for workstation-owned Stream Deck actions."""

import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional
from uuid import uuid4


@dataclass
class PendingWorkstationRequest:
    """One laptop-bound action that remains eligible for delivery."""

    request_id: str
    key: int
    device_id: str
    action_type: str
    target: str
    created_at: float
    expires_at: float
    last_attempt_at: Optional[float] = None
    attempts: int = 0


class PendingWorkstationRequests:
    """Track requests until acknowledged or until their retry window closes."""

    def __init__(
        self,
        timeout: float = 300.0,
        retry_interval: float = 5.0,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if timeout <= 0:
            raise ValueError("Pending request timeout must be positive")
        if retry_interval <= 0:
            raise ValueError("Pending request retry interval must be positive")
        self.timeout = float(timeout)
        self.retry_interval = float(retry_interval)
        self._monotonic = monotonic
        self._requests: Dict[str, PendingWorkstationRequest] = {}
        self._lock = threading.RLock()

    def add(
        self,
        key: int,
        device_id: str,
        action_type: str,
        target: str,
        *,
        replace_action_type: bool = False,
    ) -> PendingWorkstationRequest:
        """Add a request, replacing an older intent for the same key."""
        now = self._monotonic()
        with self._lock:
            for request_id, request in list(self._requests.items()):
                same_key = request.device_id == device_id and request.key == key
                same_action = (
                    replace_action_type
                    and request.device_id == device_id
                    and request.action_type == action_type
                )
                if same_key or same_action:
                    del self._requests[request_id]

            request = PendingWorkstationRequest(
                request_id=str(uuid4()),
                key=key,
                device_id=device_id,
                action_type=action_type,
                target=target,
                created_at=now,
                expires_at=now + self.timeout,
            )
            self._requests[request.request_id] = request
            return request

    def due(
        self,
        device_id: Optional[str] = None,
        *,
        force: bool = False,
    ) -> List[PendingWorkstationRequest]:
        """Return due requests and record that an attempt is starting."""
        now = self._monotonic()
        due_requests = []
        with self._lock:
            self._expire_locked(now)
            for request in self._requests.values():
                if device_id is not None and request.device_id != device_id:
                    continue
                if force or (
                    request.last_attempt_at is None
                    or now - request.last_attempt_at >= self.retry_interval
                ):
                    request.last_attempt_at = now
                    request.attempts += 1
                    due_requests.append(request)
        return due_requests

    def acknowledge(
        self,
        request_id: str,
        device_id: Optional[str] = None,
    ) -> bool:
        """Remove one request after an explicit workstation acknowledgement."""
        with self._lock:
            request = self._requests.get(str(request_id))
            if request is None:
                return False
            if device_id is not None and request.device_id != device_id:
                return False
            del self._requests[str(request_id)]
            return True

    def acknowledge_audio(
        self,
        device_id: str,
        active_target: str,
        matches: Callable[[str, str], bool],
    ) -> bool:
        """Remove audio requests confirmed by the workstation's state topic."""
        removed = False
        with self._lock:
            for request_id, request in list(self._requests.items()):
                if (
                    request.device_id == device_id
                    and request.action_type == "audio_output"
                    and matches(active_target, request.target)
                ):
                    del self._requests[request_id]
                    removed = True
        return removed

    def expire(self) -> bool:
        """Drop requests whose retry deadline has passed."""
        with self._lock:
            return self._expire_locked(self._monotonic())

    def _expire_locked(self, now: float) -> bool:
        expired = [
            request_id
            for request_id, request in self._requests.items()
            if now >= request.expires_at
        ]
        for request_id in expired:
            del self._requests[request_id]
        return bool(expired)

    def is_pending(self, device_id: str, key: int) -> bool:
        with self._lock:
            self._expire_locked(self._monotonic())
            return any(
                request.device_id == device_id and request.key == key
                for request in self._requests.values()
            )

    def snapshot(self) -> List[PendingWorkstationRequest]:
        """Return current requests for diagnostics and tests."""
        with self._lock:
            self._expire_locked(self._monotonic())
            return list(self._requests.values())
