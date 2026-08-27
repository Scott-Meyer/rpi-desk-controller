import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desk_controller.core.mqtt_client import MQTTClientHelper


class FakeMQTTClient:
    def __init__(self, *args, **kwargs):
        self.connect_result = 0
        self.loop_result = 0
        self.disconnect_result = 0
        self.subscribe_result = 0
        self.publish_result = 0
        self.subscriptions = []
        self.publishes = []
        self.disconnect_calls = 0
        self.loop_stop_calls = 0
        self.username = None
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None
        self.will = None

    def username_pw_set(self, username, password):
        self.username = (username, password)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.reconnect_delays = (min_delay, max_delay)

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, broker, port, keepalive):
        self.connect_args = (broker, port, keepalive)
        return self.connect_result

    def loop_start(self):
        return self.loop_result

    def loop_stop(self):
        self.loop_stop_calls += 1
        return self.loop_result

    def disconnect(self):
        self.disconnect_calls += 1
        return self.disconnect_result

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return self.subscribe_result, len(self.subscriptions)

    def publish(self, topic, payload, qos=0, retain=False):
        self.publishes.append((topic, payload, qos, retain))
        return SimpleNamespace(rc=self.publish_result)


class MQTTClientHelperTests(unittest.TestCase):
    def make_helper(self, **kwargs):
        client = FakeMQTTClient()
        patcher = patch(
            "desk_controller.core.mqtt_client.mqtt.Client",
            return_value=client,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        helper = MQTTClientHelper(client_id="test-client", **kwargs)
        self.addCleanup(helper.stop)
        return helper, client

    def test_subscriptions_are_applied_on_connect_and_reconnect(self):
        connected_callback = Mock()
        helper, client = self.make_helper(
            on_connect_callback=connected_callback,
        )

        self.assertTrue(helper.subscribe("desk/+/telemetry", qos=1))
        self.assertEqual(client.subscriptions, [])

        helper._on_connect(client, None, {}, 0, None)
        self.assertEqual(client.subscriptions, [("desk/+/telemetry", 1)])
        connected_callback.assert_called_once_with()

        helper._on_disconnect(client, None, {}, 1, None)
        self.assertFalse(helper.is_connected)
        helper._on_connect(client, None, {}, 0, None)

        self.assertEqual(
            client.subscriptions,
            [
                ("desk/+/telemetry", 1),
                ("desk/+/telemetry", 1),
            ],
        )
        self.assertEqual(connected_callback.call_count, 2)

    def test_connected_subscribe_and_publish_check_result_codes(self):
        helper, client = self.make_helper()
        self.assertFalse(helper.publish("desk/test", "offline"))

        helper._on_connect(client, None, {}, 0, None)
        self.assertTrue(helper.subscribe("desk/test", qos=2))
        self.assertEqual(client.subscriptions, [("desk/test", 2)])

        self.assertTrue(helper.publish("desk/test", {"ok": True}, retain=True))
        self.assertEqual(
            client.publishes[-1],
            ("desk/test", '{"ok": true}', 0, True),
        )

        client.publish_result = 4
        self.assertFalse(helper.publish("desk/test", "failure"))

    def test_connection_state_callback_only_reports_transitions(self):
        state_callback = Mock()
        helper, client = self.make_helper(
            on_connection_state_callback=state_callback,
        )

        helper._on_connect(client, None, {}, 0, None)
        helper._on_connect(client, None, {}, 0, None)
        helper._on_disconnect(client, None, {}, 1, None)
        helper._on_disconnect(client, None, {}, 1, None)

        self.assertEqual(
            state_callback.call_args_list,
            [
                unittest.mock.call(True),
                unittest.mock.call(False),
            ],
        )

    def test_start_checks_connect_and_loop_results(self):
        helper, client = self.make_helper()
        self.assertTrue(helper.start())
        self.assertEqual(
            client.connect_args,
            ("homeassistant.local", 1883, 60),
        )

        failing_helper, failing_client = self.make_helper()
        failing_client.connect_result = 4
        self.assertFalse(failing_helper.start())

    def test_invalid_qos_is_rejected(self):
        helper, _ = self.make_helper()
        with self.assertRaises(ValueError):
            helper.subscribe("desk/test", qos=3)
        with self.assertRaises(ValueError):
            helper.publish("desk/test", "payload", qos=-1)

    def test_invalid_recovery_settings_are_rejected(self):
        for timeout in (-1, float("nan"), float("inf")):
            with self.subTest(timeout=timeout), self.assertRaises(ValueError):
                self.make_helper(recovery_timeout=timeout)
        for interval in (0, -1, float("nan"), float("inf")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                self.make_helper(recovery_check_interval=interval)

    def test_last_will_is_configured_before_connect(self):
        helper, client = self.make_helper(
            will_topic="desk/workstation/availability",
            will_payload="offline",
            will_qos=1,
            will_retain=True,
        )

        self.assertEqual(
            client.will,
            ("desk/workstation/availability", "offline", 1, True),
        )
        self.assertTrue(helper.start())

    def test_prolonged_disconnect_recycles_the_network_client(self):
        old_client = FakeMQTTClient()
        replacement_client = FakeMQTTClient()
        with patch(
            "desk_controller.core.mqtt_client.mqtt.Client",
            side_effect=[old_client, replacement_client],
        ):
            helper = MQTTClientHelper(
                client_id="test-client",
                username="agent",
                password="secret",
                will_topic="desk/workstation/availability",
                will_payload="offline",
                recovery_timeout=90,
                recovery_check_interval=3600,
            )
            self.addCleanup(helper.stop)
            helper.subscribe("desk/workstation/deck/command", qos=1)

            with patch(
                "desk_controller.core.mqtt_client.time.monotonic",
                return_value=100,
            ):
                self.assertTrue(helper.start())
                helper._on_disconnect(old_client, None, {}, 1, None)

            with patch(
                "desk_controller.core.mqtt_client.time.monotonic",
                return_value=191,
            ):
                self.assertTrue(helper.recover_if_stale())

        self.assertIs(helper.client, replacement_client)
        old_client.disconnect_result = 0
        self.assertEqual(old_client.disconnect_calls, 1)
        self.assertEqual(old_client.loop_stop_calls, 1)
        self.assertEqual(
            replacement_client.connect_args,
            ("homeassistant.local", 1883, 60),
        )
        self.assertEqual(replacement_client.username, ("agent", "secret"))
        self.assertEqual(
            replacement_client.will,
            ("desk/workstation/availability", "offline", 1, True),
        )

        helper._on_connect(replacement_client, None, {}, 0, None)
        self.assertEqual(
            replacement_client.subscriptions,
            [("desk/workstation/deck/command", 1)],
        )
        self.assertTrue(helper.is_connected)

    def test_callbacks_from_recycled_client_cannot_corrupt_live_state(self):
        old_client = FakeMQTTClient()
        replacement_client = FakeMQTTClient()
        connected_callback = Mock()
        with patch(
            "desk_controller.core.mqtt_client.mqtt.Client",
            side_effect=[old_client, replacement_client],
        ):
            helper = MQTTClientHelper(
                client_id="test-client",
                on_connect_callback=connected_callback,
                recovery_timeout=0,
                recovery_check_interval=3600,
            )
            self.addCleanup(helper.stop)
            self.assertTrue(helper.start())
            self.assertTrue(helper.recover_if_stale())

        helper._on_connect(replacement_client, None, {}, 0, None)
        self.assertTrue(helper.is_connected)
        connected_callback.assert_called_once_with()

        helper._on_disconnect(old_client, None, {}, 1, None)
        helper._on_connect(old_client, None, {}, 0, None)

        self.assertTrue(helper.is_connected)
        connected_callback.assert_called_once_with()

    def test_supervisor_automatically_recovers_a_stale_connection(self):
        old_client = FakeMQTTClient()
        replacement_client = FakeMQTTClient()
        with patch(
            "desk_controller.core.mqtt_client.mqtt.Client",
            side_effect=[old_client, replacement_client],
        ):
            helper = MQTTClientHelper(
                client_id="test-client",
                recovery_timeout=0,
                recovery_check_interval=0.01,
            )
            try:
                self.assertTrue(helper.start())
                deadline = time.monotonic() + 1
                while helper.client is old_client and time.monotonic() < deadline:
                    time.sleep(0.01)

                self.assertIs(helper.client, replacement_client)
                self.assertEqual(old_client.loop_stop_calls, 1)
                self.assertEqual(
                    helper.connection_health()["recovery_count"],
                    1,
                )
            finally:
                helper.stop()

    def test_stopped_helper_restarts_with_a_fresh_client_generation(self):
        first_client = FakeMQTTClient()
        second_client = FakeMQTTClient()
        with patch(
            "desk_controller.core.mqtt_client.mqtt.Client",
            side_effect=[first_client, second_client],
        ):
            helper = MQTTClientHelper(
                client_id="test-client",
                recovery_check_interval=3600,
            )
            self.assertTrue(helper.start())
            helper._on_connect(first_client, None, {}, 0, None)
            self.assertTrue(helper.stop())

            self.assertIsNone(helper.client)
            self.assertFalse(helper.is_connected)
            self.assertTrue(helper.start())
            self.assertIs(helper.client, second_client)
            self.assertEqual(first_client.disconnect_calls, 1)
            self.assertEqual(first_client.loop_stop_calls, 1)
            helper.stop()

    def test_connection_health_reports_recovery_state(self):
        helper, client = self.make_helper(
            recovery_timeout=90,
            recovery_check_interval=3600,
        )
        self.addCleanup(helper.stop)
        with patch(
            "desk_controller.core.mqtt_client.time.monotonic",
            return_value=100,
        ):
            helper.start()
            helper._on_disconnect(client, None, {}, 7, None)
        with patch(
            "desk_controller.core.mqtt_client.time.monotonic",
            return_value=112.345,
        ):
            health = helper.connection_health()

        self.assertEqual(
            health,
            {
                "connected": False,
                "auth_failed": False,
                "auth_error": None,
                "disconnected_seconds": 12.3,
                "last_disconnect_reason": 7,
                "recovery_count": 0,
            },
        )

    def test_auth_failure_stops_loop_and_sets_auth_failed(self):
        helper, client = self.make_helper()
        helper.start()
        # Simulate CONNACK rc=4 (bad user name or password)
        helper._on_connect(client, None, {}, 4, None)

        self.assertFalse(helper.is_connected)
        self.assertTrue(helper.is_auth_failed)
        self.assertIn("bad username or password", helper.auth_error)
        self.assertEqual(client.disconnect_calls, 1)
        self.assertEqual(client.loop_stop_calls, 1)

        health = helper.connection_health()
        self.assertTrue(health["auth_failed"])
        self.assertIsNotNone(health["auth_error"])

        # Recovery should NOT run when auth failed
        self.assertFalse(helper.recover_if_stale())

    def test_auth_failure_codes_handled(self):
        for code in (4, 5, 134, 135, 138):
            with self.subTest(code=code):
                helper, client = self.make_helper()
                helper.start()
                helper._on_connect(client, None, {}, code, None)
                self.assertTrue(helper.is_auth_failed)
                self.assertFalse(helper.is_connected)

    def test_test_mqtt_connection_function(self):
        from desk_controller.core.mqtt_client import test_mqtt_connection

        fake_client = FakeMQTTClient()
        fake_client.loop_start = Mock(return_value=0)
        fake_client.connect_async = Mock(return_value=0)

        # Mock Client constructor and socket check
        with (
            patch("desk_controller.core.mqtt_client.mqtt.Client", return_value=fake_client),
            patch("desk_controller.core.mqtt_client.socket.create_connection"),
        ):
            # Test empty broker validation
            ok, msg = test_mqtt_connection(broker="", port=1883)
            self.assertFalse(ok)

            # Test successful connection
            def trigger_success(b, p, keepalive):
                fake_client.on_connect(fake_client, None, {}, 0)
                return 0

            fake_client.connect_async.side_effect = trigger_success
            ok, msg = test_mqtt_connection(broker="127.0.0.1", port=1883, timeout=1.0)
            self.assertTrue(ok)
            self.assertIn("successfully", msg)

            # Test auth failure
            def trigger_auth_fail(b, p, keepalive):
                fake_client.on_connect(fake_client, None, {}, 4)
                return 0

            fake_client.connect_async.side_effect = trigger_auth_fail
            ok, msg = test_mqtt_connection(broker="127.0.0.1", port=1883, username="bad", password="pwd", timeout=1.0)
            self.assertFalse(ok)
            self.assertIn("bad username or password", msg)


if __name__ == "__main__":
    unittest.main()
