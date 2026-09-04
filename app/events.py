"""实时事件总线；配置 Redis 后通过 Pub/Sub 跨进程广播。"""

from __future__ import annotations

import threading
import time
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from app import redis_store
from app.config import Config

logger = logging.getLogger("app.events")


@dataclass(frozen=True)
class Event:
    id: int
    event_type: str
    payload: dict


class EventBroker:
    """线程安全的有界事件日志，适用于当前单进程 P0 部署。"""

    def __init__(self, max_events: int = 1000):
        self._condition = threading.Condition()
        self._events = deque(maxlen=max_events)
        self._next_id = 0
        self._origin = uuid.uuid4().hex
        self._listener_started = False
        self._listener_lock = threading.Lock()

    def publish(self, event_type: str, payload: dict) -> Event:
        with self._condition:
            self._next_id += 1
            event = Event(self._next_id, event_type, dict(payload))
            self._events.append(event)
            self._condition.notify_all()
            self._publish_remote(event)
            return event

    def _publish_remote(self, event: Event) -> None:
        client = redis_store.get_client()
        if client is None:
            return
        try:
            client.publish(
                f"{Config.REDIS_KEY_PREFIX}events",
                json.dumps({
                    "origin": self._origin,
                    "event_type": event.event_type,
                    "payload": event.payload,
                }, ensure_ascii=False),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 发布事件失败，继续本地广播：%s", exc)

    def _start_remote_listener(self) -> None:
        if self._listener_started:
            return
        with self._listener_lock:
            if self._listener_started:
                return
            client = redis_store.get_client()
            if client is None:
                return
            self._listener_started = True
            threading.Thread(
                target=self._listen_remote,
                args=(client,),
                name="event-redis-listener",
                daemon=True,
            ).start()

    def _listen_remote(self, client) -> None:
        try:
            pubsub = client.pubsub(ignore_subscribe_messages=True)
            pubsub.subscribe(f"{Config.REDIS_KEY_PREFIX}events")
            for message in pubsub.listen():
                raw = message.get("data")
                if not raw:
                    continue
                data = json.loads(raw)
                if data.get("origin") == self._origin:
                    continue
                self._append(
                    data.get("event_type", ""),
                    data.get("payload") or {},
                )
        except Exception as exc:  # noqa: BLE001
            self._listener_started = False
            logger.warning("Redis 事件监听结束，继续本地事件：%s", exc)

    def _append(self, event_type: str, payload: dict) -> Event:
        with self._condition:
            self._next_id += 1
            event = Event(self._next_id, event_type, dict(payload))
            self._events.append(event)
            self._condition.notify_all()
            return event

    def wait_for(
        self,
        last_id: int,
        predicate: Callable[[Event], bool],
        timeout: float = 15.0,
    ) -> tuple[list[Event], int]:
        """等待新事件，同时返回最新游标，避免无关事件反复扫描。"""
        self._start_remote_listener()
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                latest_id = self._next_id
                events = [
                    event
                    for event in self._events
                    if event.id > last_id and predicate(event)
                ]
                if events or latest_id > last_id:
                    return events, latest_id

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], latest_id
                self._condition.wait(remaining)


broker = EventBroker()
