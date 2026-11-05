"""Keep USB and the monitor converging on one selected computer.

The selected computer is always the host the USB hub reports. A request
names the host the person wants; this module keeps working, USB first and
then the monitor, until the hardware gets there, retrying failures with a
bounded backoff. The latest request always wins, so nothing is dropped or
queued behind a failing attempt.

If USB moves to the other host without a request (for example, someone
switched it from another tool), that host becomes the selection and the
monitor follows it.

Consumers provide three hardware operations and receive immutable
``KVMConvergenceState`` snapshots whenever the observable state changes.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

TOGGLE = None  # ``request(TOGGLE)`` selects the other computer.


@dataclass(frozen=True)
class KVMConvergenceState:
    """What the KVM is doing, from the hardware's point of view.

    ``usb_host`` is the last fresh reading from the hub (``0`` = PC1,
    ``1`` = PC2, ``None`` = unreadable). ``target`` is the host being worked
    towards. ``monitor_host`` is the host the monitor was last successfully
    sent to; ``None`` means it has not been sent to any host yet.
    ``retrying`` explains that the last attempt failed and another follows.
    """

    usb_host: Optional[int]
    target: Optional[int]
    monitor_host: Optional[int]
    retrying: bool = False
    detail: str = ""

    @property
    def pending(self) -> bool:
        """True while USB or the monitor has not yet reached the target."""
        return self.target is not None and (
            self.usb_host != self.target or self.monitor_host != self.target
        )


class KVMConvergence:
    """Drive USB, then the monitor, toward the latest requested host.

    ``read_usb()`` returns a fresh hub reading or ``None``. ``set_usb(host)``
    and ``set_monitor(host)`` return whether the hardware accepted the
    command. A USB step only counts once ``read_usb`` confirms it. The
    monitor step counts when ``set_monitor`` succeeds, because monitors may
    have no trustworthy readback. ``on_change`` receives each new state on
    the worker thread; it must not call back into ``request`` synchronously
    expecting the state to have advanced.
    """

    def __init__(
        self,
        *,
        read_usb: Callable[[], Optional[int]],
        set_usb: Callable[[int], bool],
        set_monitor: Callable[[int], bool],
        on_change: Callable[[KVMConvergenceState], None] = lambda _state: None,
        poll_interval: float = 5.0,
        initial_retry: float = 1.0,
        max_retry: float = 30.0,
    ):
        self._read_usb = read_usb
        self._set_usb = set_usb
        self._set_monitor = set_monitor
        self._on_change = on_change
        self._poll_interval = poll_interval
        self._initial_retry = initial_retry
        self._max_retry = max_retry

        self._lock = threading.Lock()
        self._changed = threading.Condition(self._lock)
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._usb_host: Optional[int] = None
        self._target: Optional[int] = None
        self._monitor_host: Optional[int] = None
        self._toggle_pending = False
        # The host USB was last confirmed at for the current target. A later
        # different reading without a new request is an outside change.
        self._usb_confirmed: Optional[int] = None
        # Bumped by every request so a hardware step started for an older
        # request can never confirm a newer one.
        self._generation = 0
        # Generation current when the latest USB reading was taken. A reading
        # taken before a request says nothing about that request's outcome.
        self._read_generation = -1
        self._retrying = False
        self._detail = ""
        self._reported: Optional[KVMConvergenceState] = None

    def start(self) -> None:
        """Begin observing USB and converging. Safe to call once."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run, name="kvm-convergence", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def request(self, host: Optional[int]) -> int:
        """Select ``0``/``1``, or ``TOGGLE`` relative to the current selection.

        Returns immediately; work happens on the convergence thread. A toggle
        while a switch is still in progress turns back toward where it came
        from, so a second press always means "the other one". Every request
        sends the monitor its host again, even if it was already selected,
        because the monitor may have been changed by hand. Returns the
        request's generation for ``wait_for_usb``.
        """
        if host not in (0, 1, TOGGLE):
            raise ValueError("KVM host must be 0, 1, or TOGGLE")
        with self._lock:
            if host is TOGGLE:
                base = self._target if self._target is not None else self._usb_host
                if base is None:
                    # Resolve once the hub has been read.
                    self._toggle_pending = not self._toggle_pending
                    host = None
                else:
                    host = 1 - base
                    self._toggle_pending = False
            else:
                self._toggle_pending = False
            if host is not None:
                self._target = host
            self._generation += 1
            self._usb_confirmed = None
            self._monitor_host = None
            self._retrying = False
            self._detail = ""
            generation = self._generation
            self._changed.notify_all()
        self._wake.set()
        return generation

    def retry_now(self) -> None:
        """Skip any retry backoff, e.g. when a hardware owner comes back."""
        self._wake.set()

    def wait_for_usb(self, host: int, generation: int, timeout: float) -> bool:
        """Wait until a reading taken after request ``generation`` shows ``host``.

        Returns False if a newer request supersedes it or ``timeout`` elapses.
        For synchronous callers only; never call from ``on_change``.
        """
        with self._changed:
            self._changed.wait_for(
                lambda: (
                    self._generation != generation
                    or (self._read_generation == generation and self._usb_host == host)
                ),
                timeout,
            )
            return (
                self._generation == generation
                and self._usb_host == host
                and (self._read_generation == generation)
            )

    def state(self) -> KVMConvergenceState:
        with self._lock:
            return self._snapshot()

    def _snapshot(self) -> KVMConvergenceState:
        return KVMConvergenceState(
            usb_host=self._usb_host,
            target=self._target,
            monitor_host=self._monitor_host,
            retrying=self._retrying,
            detail=self._detail,
        )

    def _report(self) -> None:
        with self._lock:
            state = self._snapshot()
            self._changed.notify_all()
            if state == self._reported:
                return
            self._reported = state
        try:
            self._on_change(state)
        except Exception:
            logger.exception("KVM state listener failed")

    def _read(self) -> Optional[int]:
        with self._lock:
            generation = self._generation
        reading = self._read_usb()
        with self._lock:
            self._usb_host = reading
            self._read_generation = generation
            self._changed.notify_all()
        return reading

    def _observe(self) -> None:
        reading = self._read()
        with self._lock:
            if reading is None or self._read_generation != self._generation:
                # Unreadable, or taken before the latest request: it can
                # neither confirm nor redefine that request.
                return
            if self._toggle_pending:
                self._toggle_pending = False
                self._target = 1 - reading
            elif self._target is None:
                # Nothing requested yet: the hub's host is the selection.
                self._target = reading
                self._usb_confirmed = reading
            elif self._usb_confirmed == self._target and reading != self._target:
                logger.info(
                    "USB moved to PC%s without a request; selecting it",
                    reading + 1,
                )
                self._target = reading
                self._usb_confirmed = reading
            elif reading == self._target:
                self._usb_confirmed = reading

    def _fail(self, detail: str) -> None:
        with self._lock:
            self._retrying = True
            self._detail = detail
        logger.warning("%s; retrying", detail)

    def _succeed(self) -> None:
        with self._lock:
            self._retrying = False
            self._detail = ""

    def _step(self) -> Optional[bool]:
        """Do one unit of work. True = progressed, False = failed, None = idle."""
        with self._lock:
            target, usb, monitor = self._target, self._usb_host, self._monitor_host
            generation = self._generation
            stale = self._read_generation != generation
        if stale:
            return True  # re-read USB for the latest request before acting
        if target is None:
            return None
        if usb != target:
            accepted = self._set_usb(target)
            reading = self._read()
            with self._lock:
                if reading == target and generation == self._generation:
                    self._usb_confirmed = target
            if reading == target:
                self._succeed()
                return True
            self._fail(
                f"USB did not reach PC{target + 1} (hub "
                f"{'accepted' if accepted else 'refused'} the switch, reads "
                f"{'unknown' if reading is None else f'PC{reading + 1}'})"
            )
            return False
        if monitor != target:
            with self._lock:
                # A write that fails may still have changed the display, so
                # whatever it showed before is no longer known.
                if generation == self._generation:
                    self._monitor_host = None
            if self._set_monitor(target):
                with self._lock:
                    # A newer request may have arrived while the monitor was
                    # being written; it must still send its own command.
                    if generation == self._generation:
                        self._monitor_host = target
                self._succeed()
                return True
            self._fail(f"Monitor did not accept PC{target + 1}")
            return False
        return None

    def _run(self) -> None:
        delay = self._initial_retry
        while not self._stop.is_set():
            try:
                self._observe()
                self._report()
                outcome = self._step()
            except Exception:
                logger.exception("KVM convergence step failed")
                self._fail("KVM hardware step raised an error")
                outcome = False
            self._report()
            if outcome is True:
                delay = self._initial_retry
                continue
            if outcome is False:
                wait, delay = delay, min(delay * 2, self._max_retry)
            else:
                wait, delay = self._poll_interval, self._initial_retry
            woke = self._wake.wait(wait)
            self._wake.clear()
            if woke:
                delay = self._initial_retry
