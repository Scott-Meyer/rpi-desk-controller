"""The selected computer is where USB is, and requests always get there.

These tests drive ``KVMConvergence`` against small fake hardware that has
real state (which host the hub is on, what the monitor shows), then check
that state rather than the calls that produced it.
"""

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import uuid4

from desk_controller.core.kvm import KVMHardwareResult
from desk_controller.pi_controller.drivers.acroname_hub import AcronameHubController
from desk_controller.pi_controller.kvm_convergence import TOGGLE, KVMConvergence
from desk_controller.pi_controller.main import DeskControllerApp


def eventually(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


class FakeHub:
    def __init__(self, host=0):
        self.host = host
        self.refuse = 0  # refuse this many switches
        self.unreadable = False
        self.gate = None  # Event that holds a switch until set
        self.read_gate = None  # Event that holds a reading already taken

    def read(self):
        value = None if self.unreadable else self.host
        if self.read_gate is not None:
            self.read_gate.wait(2)
        return value

    def switch(self, host):
        if self.gate is not None:
            self.gate.wait(2)
        if self.refuse:
            self.refuse -= 1
            return False
        self.host = host
        return True


class FakeMonitor:
    def __init__(self, shows=0):
        self.shows = shows
        self.refuse = 0  # ignore and refuse this many writes
        self.apply_then_fail = 0  # change input but report failure

    def set(self, host):
        if self.refuse:
            self.refuse -= 1
            return False
        self.shows = host
        if self.apply_then_fail:
            self.apply_then_fail -= 1
            return False
        return True


class KVMConvergenceTests(unittest.TestCase):
    def start(self, hub, monitor, **kwargs):
        self.states = []
        engine = KVMConvergence(
            read_usb=hub.read,
            set_usb=hub.switch,
            set_monitor=monitor.set,
            on_change=self.states.append,
            poll_interval=kwargs.pop("poll_interval", 0.02),
            initial_retry=0.01,
            max_retry=0.05,
        )
        engine.start()
        self.addCleanup(engine.stop)
        return engine

    def settled(self, engine, hub, monitor, host):
        return eventually(
            lambda: hub.host == host
            and monitor.shows == host
            and engine.state().usb_host == host
            and not engine.state().pending
        )

    def test_selection_is_the_hub_host_and_the_monitor_follows_on_startup(self):
        hub, monitor = FakeHub(host=1), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)

        self.assertTrue(self.settled(engine, hub, monitor, 1))
        self.assertEqual(engine.state().target, 1)

    def test_failing_usb_and_monitor_are_retried_until_both_switch(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        hub.refuse, monitor.refuse = 3, 4

        engine.request(1)

        self.assertTrue(self.settled(engine, hub, monitor, 1))
        self.assertTrue(any(state.retrying for state in self.states))
        self.assertFalse(engine.state().retrying)

    def test_selection_shows_usb_host_while_the_monitor_is_still_retrying(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        monitor.refuse = 10_000

        engine.request(1)

        self.assertTrue(eventually(lambda: engine.state().usb_host == 1))
        state = engine.state()
        self.assertTrue(state.pending)
        self.assertEqual(monitor.shows, 0)
        monitor.refuse = 0
        self.assertTrue(self.settled(engine, hub, monitor, 1))

    def test_a_second_toggle_during_a_switch_turns_back(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        hub.gate = threading.Event()

        engine.request(TOGGLE)
        engine.request(TOGGLE)
        hub.gate.set()

        self.assertTrue(self.settled(engine, hub, monitor, 0))

    def test_latest_request_wins_over_a_write_still_in_flight(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        hub.gate = threading.Event()

        engine.request(1)
        self.assertTrue(eventually(lambda: engine.state().target == 1))
        time.sleep(0.05)  # let the worker block inside switch(1)
        engine.request(0)
        hub.gate.set()

        self.assertTrue(self.settled(engine, hub, monitor, 0))

    def test_usb_moved_elsewhere_becomes_the_selection_and_monitor_follows(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))

        hub.host = 1  # someone switched the hub from another tool

        self.assertTrue(self.settled(engine, hub, monitor, 1))

    def test_reselecting_after_outside_usb_drift_goes_back(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor, poll_interval=60)
        self.assertTrue(self.settled(engine, hub, monitor, 0))

        hub.host = 1  # drifts between polls
        engine.request(0)

        self.assertTrue(self.settled(engine, hub, monitor, 0))

    def test_a_reading_taken_before_a_request_cannot_satisfy_it(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        gate = hub.read_gate = threading.Event()
        time.sleep(0.05)  # the worker's next poll has read PC1 and is held

        hub.host = 1  # USB drifts
        engine.request(0)  # and the person asks for PC1 again
        hub.read_gate = None
        gate.set()  # the held reading (PC1, taken before the request) lands

        self.assertTrue(self.settled(engine, hub, monitor, 0))

    def test_reselecting_the_current_computer_resends_the_monitor(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))

        monitor.shows = 1  # changed by hand on the monitor's own menu
        engine.request(0)

        self.assertTrue(eventually(lambda: monitor.shows == 0))

    def test_reversal_restores_a_monitor_that_switched_but_reported_failure(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        monitor.apply_then_fail = 1

        engine.request(1)
        self.assertTrue(eventually(lambda: monitor.shows == 1))
        engine.request(0)

        self.assertTrue(self.settled(engine, hub, monitor, 0))

    def test_unreadable_hub_is_unknown_and_never_counts_as_switched(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        hub.unreadable = True

        engine.request(1)

        self.assertTrue(eventually(lambda: engine.state().retrying))
        self.assertIsNone(engine.state().usb_host)
        self.assertEqual(monitor.shows, 0)  # never moved without USB proof
        hub.unreadable = False
        self.assertTrue(self.settled(engine, hub, monitor, 1))

    def test_waiting_ignores_a_reading_taken_before_the_request(self):
        hub, monitor = FakeHub(host=0), FakeMonitor(shows=0)
        engine = self.start(hub, monitor)
        self.assertTrue(self.settled(engine, hub, monitor, 0))
        hub.gate = threading.Event()
        engine.request(1)
        time.sleep(0.05)  # worker is now blocked inside switch(1)

        generation = engine.request(0)
        self.assertFalse(engine.wait_for_usb(0, generation, timeout=0.1))
        hub.gate.set()

        self.assertTrue(engine.wait_for_usb(0, generation, timeout=2))
        self.assertTrue(self.settled(engine, hub, monitor, 0))


class AcronameUpstreamReadTests(unittest.TestCase):
    @patch(
        "desk_controller.pi_controller.drivers.acroname_hub.BRAINSTEM_AVAILABLE", True
    )
    def test_failed_hub_reading_is_unknown_not_the_last_command(self):
        from desk_controller.pi_controller.drivers import acroname_hub

        hub = AcronameHubController()
        hub.connected = True
        hub.stem = Mock()
        ok = acroname_hub.Result.NO_ERROR
        hub.stem.usb.setUpstreamMode.return_value = ok
        hub.stem.usb.getUpstreamState.return_value = Mock(error=ok + 1, value=None)

        self.assertTrue(hub.switch_upstream_channel(1))

        self.assertIsNone(hub.read_upstream_channel())


class AltInputMonitor:
    """An LG alt-input monitor: accepts writes, has no usable readback."""

    def __init__(self):
        self.shows = None
        self.refuse = False

    def set_input_source(self, code):
        if self.refuse:
            return False
        self.shows = code
        return True

    def get_input_source(self):
        return None


class SelectedComputerFollowsUSBTests(unittest.TestCase):
    """Through the controller: the selected computer is the hub's host."""

    @patch(
        "desk_controller.pi_controller.main.resolve_config_path",
        return_value=Path("/tmp/desk-test.yaml"),
    )
    @patch("desk_controller.pi_controller.main.load_config")
    def make_app(self, load, _path, *, hub_host, kvm=None, remote_step=None):
        load.return_value = {
            "hardware": {"simulate": True},
            "homeassistant": {"enabled": False},
            "usb_switch": {"driver": "acroname", "default_channel": 0},
            "kvm": kvm or {},
            "monitors": [
                {
                    "display_id": 1,
                    "inputs": {"pc1": "0xd0", "pc2": "0x91"},
                    "use_alt_addressing": True,
                }
            ],
            "workstations": {"pc1": "windows-id", "pc2": "mac-id"},
        }
        app = DeskControllerApp()
        app.mqtt = Mock()
        app.mqtt.publish.return_value = True
        app._update_sd_keys = Mock()
        app.monitor = AltInputMonitor()
        app.acroname.active_channel = hub_host
        if remote_step is not None:
            app._execute_remote_kvm_step = Mock(side_effect=remote_step)
        app._kvm_convergence.start()
        self.addCleanup(app._kvm_convergence.stop)
        return app

    def published_host(self, app):
        states = [
            c.args[1]
            for c in app.mqtt.publish.call_args_list
            if c.args[0] == "desk/kvm/state"
        ]
        return states[-1] if states else None

    def test_host_key_and_state_follow_usb_even_when_the_monitor_fails(self):
        app = self.make_app(hub_host=1)
        app.monitor.refuse = True
        self.assertTrue(eventually(lambda: self.published_host(app) == "PC2"))
        self.assertEqual(app.current_pc, 1)

        app._start_kvm_switch(None)  # the Stream Deck host key

        self.assertTrue(eventually(lambda: self.published_host(app) == "PC1"))
        self.assertEqual(app.acroname.active_channel, 0)
        self.assertEqual(app.current_pc, 0)
        self.assertFalse(app.kvm_fault)
        key = app._status_visual(0, {"action_type": "kvm_toggle"}).as_dict()
        self.assertEqual(key["observed"], "PC 1")

        app.monitor.refuse = False
        app._reconcile_kvm_state()
        self.assertTrue(eventually(lambda: app.monitor.shows == "0xd0"))

    def test_agent_owned_monitor_is_sent_by_the_computer_it_is_showing(self):
        app = self.make_app(
            hub_host=0,
            kvm={"monitor_controller": "active_workstation", "usb_controller": "pi"},
            remote_step=lambda owner, transaction_id, operation, *args: (
                KVMHardwareResult(
                    transaction_id=transaction_id,
                    command_id=uuid4(),
                    operation=operation,
                    success=True,
                )
            ),
        )
        self.assertTrue(eventually(lambda: not app._kvm_convergence.state().pending))

        app._start_kvm_switch(1)

        self.assertTrue(eventually(lambda: app.acroname.active_channel == 1))
        self.assertTrue(eventually(lambda: not app._kvm_convergence.state().pending))
        owner, _transaction, *rest = app._execute_remote_kvm_step.call_args.args
        self.assertEqual(owner, "windows-id")  # PC1 was showing
        self.assertEqual(rest, ["monitor_set", 1])

    def test_agent_owned_monitor_changed_by_hand_is_still_reachable(self):
        reachable = {"windows-id"}
        sent_by = []

        def remote_step(owner, transaction_id, operation, *args):
            sent_by.append(owner)
            return KVMHardwareResult(
                transaction_id=transaction_id,
                command_id=uuid4(),
                operation=operation,
                success=owner in reachable,
            )

        app = self.make_app(
            hub_host=0,
            kvm={"monitor_controller": "active_workstation", "usb_controller": "pi"},
            remote_step=remote_step,
        )
        self.assertTrue(eventually(lambda: not app._kvm_convergence.state().pending))
        # Someone switches the monitor to PC2 by hand, so only PC2's agent
        # can reach it now.
        reachable = {"mac-id"}
        sent_by.clear()

        app._start_kvm_switch(0)  # reselect PC1, which USB is already on

        self.assertTrue(eventually(lambda: "mac-id" in sent_by))
        self.assertTrue(eventually(lambda: not app._kvm_convergence.state().pending))


if __name__ == "__main__":
    unittest.main()
