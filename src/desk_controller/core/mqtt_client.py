"""Shared MQTT client with reconnect-safe subscriptions and checked operations."""

import json
import logging
import math
import threading
import time
from typing import Any, Callable, Dict, Optional

import paho.mqtt.client as mqtt

logger = logging.getLogger("MQTTClient")


class MQTTClientHelper:
    """Small paho-mqtt wrapper shared by the Pi and desktop agents."""

    DEFAULT_RECOVERY_TIMEOUT = 90.0
    DEFAULT_RECOVERY_CHECK_INTERVAL = 5.0

    def __init__(
        self,
        client_id: str,
        broker: str = "homeassistant.local",
        port: int = 1883,
        username: Optional[str] = None,
        password: Optional[str] = None,
        on_message_callback: Optional[Callable[[str, str], None]] = None,
        on_connect_callback: Optional[Callable[[], None]] = None,
        on_connection_state_callback: Optional[Callable[[bool], None]] = None,
        will_topic: Optional[str] = None,
        will_payload: Any = None,
        will_qos: int = 1,
        will_retain: bool = True,
        recovery_timeout: float = DEFAULT_RECOVERY_TIMEOUT,
        recovery_check_interval: float = DEFAULT_RECOVERY_CHECK_INTERVAL,
    ):
        if not math.isfinite(recovery_timeout) or recovery_timeout < 0:
            raise ValueError("MQTT recovery timeout must be finite and non-negative")
        if not math.isfinite(recovery_check_interval) or recovery_check_interval <= 0:
            raise ValueError("MQTT recovery check interval must be finite and positive")
        if not 0 <= will_qos <= 2:
            raise ValueError("MQTT Last Will QoS must be 0, 1, or 2")

        self.client_id = client_id
        self.broker = broker
        self.port = port
        self.username = username
        self.password = password
        self.on_message_callback = on_message_callback
        self.on_connect_callback = on_connect_callback
        self.on_connection_state_callback = on_connection_state_callback
        self.will_topic = will_topic
        self.will_payload = will_payload
        self.will_qos = will_qos
        self.will_retain = will_retain
        self.recovery_timeout = float(recovery_timeout)
        self.recovery_check_interval = float(recovery_check_interval)

        self._is_connected = False
        self._loop_started = False
        self._running = False
        self._stopping = False
        self._disconnected_since: Optional[float] = None
        self._last_disconnect_reason: Optional[int] = None
        self._recovery_count = 0
        self._connected_event = threading.Event()
        self._subscriptions: Dict[str, int] = {}
        self._subscriptions_lock = threading.RLock()
        self._state_lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._recovery_lock = threading.Lock()
        self._supervisor_stop = threading.Event()
        self._supervisor_thread: Optional[threading.Thread] = None

        self.client = self._create_client()

    def _create_client(self):
        """Create one fully configured Paho client generation."""
        if hasattr(mqtt, "CallbackAPIVersion"):
            client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2,
                client_id=self.client_id,
            )
        else:
            client = mqtt.Client(client_id=self.client_id)

        if self.username is not None:
            client.username_pw_set(self.username, self.password)

        if self.will_topic is not None:
            will_body = (
                json.dumps(self.will_payload)
                if isinstance(self.will_payload, (dict, list))
                else str(self.will_payload)
            )
            client.will_set(
                self.will_topic,
                will_body,
                qos=self.will_qos,
                retain=self.will_retain,
            )

        client.reconnect_delay_set(min_delay=1, max_delay=30)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    @staticmethod
    def _result_code_value(result_code: Any) -> int:
        return int(getattr(result_code, "value", result_code))

    @classmethod
    def _operation_succeeded(cls, result_code: Any) -> bool:
        return cls._result_code_value(result_code) == mqtt.MQTT_ERR_SUCCESS

    def _notify_connection_state(self, connected: bool) -> None:
        callback = self.on_connection_state_callback
        if callback is None:
            return
        try:
            callback(connected)
        except Exception:
            logger.exception("MQTT connection-state callback failed")

    def _subscribe_now(self, topic: str, qos: int, client=None) -> bool:
        active_client = client if client is not None else self.client
        if active_client is None or active_client is not self.client:
            return False
        result_code, message_id = active_client.subscribe(topic, qos=qos)
        if not self._operation_succeeded(result_code):
            logger.error(
                "Failed subscribing to MQTT topic '%s' (qos=%s, rc=%s)",
                topic,
                qos,
                self._result_code_value(result_code),
            )
            return False

        logger.info(
            "Subscribed to MQTT topic '%s' (qos=%s, mid=%s)",
            topic,
            qos,
            message_id,
        )
        return True

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if client is not self.client:
            logger.debug("Ignoring connect callback from retired MQTT client")
            return

        if not self._operation_succeeded(reason_code):
            now = time.monotonic()
            with self._state_lock:
                was_connected = self._is_connected
                self._is_connected = False
                if self._disconnected_since is None:
                    self._disconnected_since = now
                self._last_disconnect_reason = self._result_code_value(reason_code)
                self._connected_event.clear()
            logger.error(
                "Failed to connect to MQTT broker with code %s",
                self._result_code_value(reason_code),
            )
            if was_connected:
                self._notify_connection_state(False)
            return

        with self._state_lock:
            was_connected = self._is_connected
            self._is_connected = True
            self._disconnected_since = None
            self._connected_event.set()
        logger.info(
            "Connected to MQTT broker at %s:%s as '%s'",
            self.broker,
            self.port,
            self.client_id,
        )

        with self._subscriptions_lock:
            subscriptions = list(self._subscriptions.items())
        for topic, qos in subscriptions:
            self._subscribe_now(topic, qos, client=client)

        if client is self.client and self.on_connect_callback:
            try:
                self.on_connect_callback()
            except Exception:
                logger.exception("MQTT on-connect callback failed")
        if not was_connected:
            self._notify_connection_state(True)

    def _on_disconnect(self, client, userdata, *callback_args):
        if client is not self.client:
            logger.debug("Ignoring disconnect callback from retired MQTT client")
            return

        if len(callback_args) >= 2:
            reason_code = callback_args[1]
        elif callback_args:
            reason_code = callback_args[0]
        else:
            reason_code = mqtt.MQTT_ERR_SUCCESS

        reason_value = self._result_code_value(reason_code)
        now = time.monotonic()
        with self._state_lock:
            was_connected = self._is_connected
            self._is_connected = False
            if self._disconnected_since is None:
                self._disconnected_since = now
            self._last_disconnect_reason = reason_value
            self._connected_event.clear()
        if self._operation_succeeded(reason_code):
            logger.info("Disconnected from MQTT broker")
        else:
            logger.warning(
                "Disconnected from MQTT broker (rc=%s); paho will reconnect",
                reason_value,
            )
        if was_connected:
            self._notify_connection_state(False)

    def _on_message(self, client, userdata, msg):
        if client is not self.client:
            logger.debug("Ignoring message callback from retired MQTT client")
            return
        try:
            payload = msg.payload.decode("utf-8")
            topic = msg.topic
            logger.debug("MQTT message received on '%s': %s", topic, payload)
            if self.on_message_callback:
                self.on_message_callback(topic, payload)
        except Exception:
            logger.exception("Error handling MQTT message on %s", msg.topic)

    @property
    def is_connected(self) -> bool:
        with self._state_lock:
            return self._is_connected

    def connection_health(self) -> Dict[str, Any]:
        """Return secret-free connection and recovery diagnostics."""
        now = time.monotonic()
        with self._state_lock:
            disconnected_seconds = (
                None
                if self._is_connected or self._disconnected_since is None
                else round(max(0.0, now - self._disconnected_since), 1)
            )
            return {
                "connected": self._is_connected,
                "disconnected_seconds": disconnected_seconds,
                "last_disconnect_reason": self._last_disconnect_reason,
                "recovery_count": self._recovery_count,
            }

    def _start_network_loop(self, client) -> bool:
        """Start one Paho client generation without starting the supervisor."""
        try:
            connect_result = client.connect_async(
                self.broker,
                self.port,
                keepalive=60,
            )
            if connect_result is not None and not self._operation_succeeded(
                connect_result
            ):
                logger.error(
                    "MQTT connect_async failed (rc=%s)",
                    self._result_code_value(connect_result),
                )
                return False

            loop_result = client.loop_start()
            if loop_result is not None and not self._operation_succeeded(loop_result):
                logger.error(
                    "MQTT loop_start failed (rc=%s)",
                    self._result_code_value(loop_result),
                )
                return False
        except Exception:
            logger.exception("MQTT connection setup failed")
            return False

        self._loop_started = True
        return True

    def _start_supervisor(self) -> None:
        if self._supervisor_thread and self._supervisor_thread.is_alive():
            return
        self._supervisor_stop.clear()
        self._supervisor_thread = threading.Thread(
            target=self._supervise_connection,
            name=f"mqtt-supervisor-{self.client_id}",
            daemon=True,
        )
        self._supervisor_thread.start()

    def _supervise_connection(self) -> None:
        while not self._supervisor_stop.wait(self.recovery_check_interval):
            try:
                self.recover_if_stale()
            except Exception:
                logger.exception("MQTT recovery supervisor failed")

    def start(self) -> bool:
        """Start paho's network loop and begin connecting asynchronously."""
        with self._lifecycle_lock:
            if self._running:
                return True

            logger.info("Connecting to MQTT broker %s:%s...", self.broker, self.port)
            self._stopping = False
            if self.client is None:
                self.client = self._create_client()
            with self._state_lock:
                self._is_connected = False
                self._disconnected_since = time.monotonic()
                self._connected_event.clear()

            if not self._start_network_loop(self.client):
                failed_client = self.client
                self.client = None
                self._shutdown_client(
                    failed_client,
                    loop_started=False,
                    disconnect=True,
                )
                with self._state_lock:
                    self._disconnected_since = None
                return False

            self._running = True
            self._start_supervisor()
            return True

    def recover_if_stale(self) -> bool:
        """Replace a client that Paho could not reconnect within the deadline."""
        now = time.monotonic()
        with self._state_lock:
            disconnected_since = self._disconnected_since
            should_recover = (
                self._running
                and not self._stopping
                and not self._is_connected
                and disconnected_since is not None
                and now - disconnected_since >= self.recovery_timeout
            )
        if not should_recover:
            return False
        if not self._recovery_lock.acquire(blocking=False):
            return False

        try:
            with self._lifecycle_lock:
                now = time.monotonic()
                with self._state_lock:
                    disconnected_since = self._disconnected_since
                    if (
                        not self._running
                        or self._stopping
                        or self._is_connected
                        or disconnected_since is None
                        or now - disconnected_since < self.recovery_timeout
                    ):
                        return False

                logger.warning(
                    (
                        "MQTT remained disconnected for %.1fs; replacing client "
                        "generation %s"
                    ),
                    now - disconnected_since,
                    self._recovery_count + 1,
                )
                replacement = self._create_client()
                retired = self.client
                retired_loop_started = self._loop_started
                self.client = replacement
                self._loop_started = False
                with self._state_lock:
                    self._is_connected = False
                    self._disconnected_since = now
                    self._recovery_count += 1
                    self._connected_event.clear()

                if retired is not None:
                    self._shutdown_client(
                        retired,
                        loop_started=retired_loop_started,
                        disconnect=True,
                    )
                if not self._start_network_loop(replacement):
                    logger.error("Replacement MQTT client failed to start")
                return True
        finally:
            self._recovery_lock.release()

    def wait_until_connected(self, timeout: float = 10.0) -> bool:
        """Wait until the broker accepts the connection."""
        return self._connected_event.wait(timeout)

    def subscribe(self, topic: str, qos: int = 0) -> bool:
        """Remember a subscription and apply it now or on the next connection."""
        if not 0 <= qos <= 2:
            raise ValueError("MQTT QoS must be 0, 1, or 2")

        with self._subscriptions_lock:
            self._subscriptions[topic] = qos

        if not self.is_connected:
            logger.info(
                "Queued MQTT subscription for '%s' (qos=%s) until connected",
                topic,
                qos,
            )
            return True

        return self._subscribe_now(topic, qos)

    def publish(
        self, topic: str, payload: Any, retain: bool = False, qos: int = 0
    ) -> bool:
        """Publish a payload and report whether paho accepted the operation."""
        if not 0 <= qos <= 2:
            raise ValueError("MQTT QoS must be 0, 1, or 2")
        if not self.is_connected:
            logger.warning("Cannot publish to '%s': MQTT is not connected", topic)
            return False

        body = (
            json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload)
        )
        try:
            active_client = self.client
            if active_client is None:
                return False
            message_info = active_client.publish(
                topic,
                body,
                qos=qos,
                retain=retain,
            )
        except Exception:
            logger.exception("MQTT publish to '%s' raised an exception", topic)
            return False

        if not self._operation_succeeded(message_info.rc):
            logger.error(
                "MQTT publish to '%s' failed (rc=%s)",
                topic,
                self._result_code_value(message_info.rc),
            )
            return False
        return True

    def _shutdown_client(self, client, loop_started: bool, disconnect: bool) -> bool:
        success = True
        if disconnect:
            try:
                disconnect_result = client.disconnect()
                no_connection = getattr(mqtt, "MQTT_ERR_NO_CONN", None)
                if (
                    disconnect_result is not None
                    and not self._operation_succeeded(disconnect_result)
                    and disconnect_result != no_connection
                ):
                    logger.error(
                        "MQTT disconnect failed (rc=%s)",
                        self._result_code_value(disconnect_result),
                    )
                    success = False
            except Exception:
                logger.exception("MQTT disconnect raised an exception")
                success = False

        if loop_started:
            try:
                loop_result = client.loop_stop()
                if loop_result is not None and not self._operation_succeeded(
                    loop_result
                ):
                    logger.error(
                        "MQTT loop_stop failed (rc=%s)",
                        self._result_code_value(loop_result),
                    )
                    success = False
            except Exception:
                logger.exception("MQTT loop_stop raised an exception")
                success = False
        return success

    def stop(self) -> bool:
        """Stop recovery supervision and retire the current Paho client."""
        with self._lifecycle_lock:
            if not self._running and not self._loop_started:
                return True
            self._running = False
            self._stopping = True
            self._supervisor_stop.set()
            supervisor = self._supervisor_thread
            self._supervisor_thread = None
            retired = self.client
            retired_loop_started = self._loop_started
            self.client = None
            self._loop_started = False
            with self._state_lock:
                was_connected = self._is_connected
                self._is_connected = False
                self._disconnected_since = None
                self._connected_event.clear()

        if was_connected:
            self._notify_connection_state(False)

        if (
            supervisor
            and supervisor.is_alive()
            and supervisor is not threading.current_thread()
        ):
            supervisor.join(timeout=5.0)
            if supervisor.is_alive():
                logger.error("MQTT recovery supervisor did not stop")

        success = self._shutdown_client(
            retired,
            loop_started=retired_loop_started,
            disconnect=was_connected or retired_loop_started,
        )
        with self._lifecycle_lock:
            self._stopping = False
        return success
