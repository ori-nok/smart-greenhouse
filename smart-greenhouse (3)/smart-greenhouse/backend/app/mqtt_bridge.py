"""Транспорт MQTT (paho-mqtt 2.x): подписка на топики устройств, публикация команд и конфигурации.
Автоматическое переподключение; ошибки обработчика не останавливают сетевой цикл."""
import logging
import os
import threading
import uuid
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from .config import Settings
from .protocol import Topics
from .service import Publisher

log = logging.getLogger("greenhouse.mqtt")


class MqttBridge(Publisher):
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.topics = Topics(settings.topic_root)
        self._connected = threading.Event()
        self._on_message: Optional[Callable] = None
        self._on_connected: Optional[Callable] = None
        # Идентификатор клиента должен быть уникальным на брокере: на публичном брокере второй клиент с тем же id
        # отключает первого, и сообщения теряются. Можно задать вручную через MQTT_CLIENT_ID.
        client_id = os.environ.get("MQTT_CLIENT_ID") or f"gh-backend-{uuid.uuid4().hex[:10]}"
        log.info("MQTT client id: %s", client_id)
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        self.client.reconnect_delay_set(min_delay=1, max_delay=15)
        self.client.on_connect = self._handle_connect
        self.client.on_disconnect = self._handle_disconnect
        self.client.on_message = self._handle_message

    def set_handlers(self, on_message: Callable, on_connected: Callable) -> None:
        self._on_message, self._on_connected = on_message, on_connected

    def start(self) -> None:
        self.client.connect_async(self.s.mqtt_host, self.s.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()

    def is_connected(self) -> bool:
        return self._connected.is_set()

    def publish(self, topic: str, payload: str, retain: bool = False, qos: int = 1) -> None:
        info = self.client.publish(topic, payload, qos=qos, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish to %s failed: rc=%s", topic, info.rc)

    def _handle_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            log.error("MQTT connect refused: %s", reason_code)
            return
        for t in self.topics.subscriptions():
            client.subscribe(t, qos=1)
        self._connected.set()
        log.info("MQTT connected to %s:%s", self.s.mqtt_host, self.s.mqtt_port)
        try:
            if self._on_connected:
                self._on_connected()
        except Exception:
            log.exception("on_connected handler failed")

    def _handle_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        self._connected.clear()
        log.warning("MQTT disconnected: %s", reason_code)

    def _handle_message(self, client, userdata, msg) -> None:
        try:
            if self._on_message:
                self._on_message(msg.topic, msg.payload, bool(msg.retain))
        except Exception:
            log.exception("message handler failed (topic=%s)", msg.topic)
