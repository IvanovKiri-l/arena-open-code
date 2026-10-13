"""Шина событий забега для SSE-стримов.

Забег публикует события (чанки, готовые ответы, вердикт), а подписчики
получают их в реальном времени. Хранится в памяти процесса — этого
достаточно для однопроцессной сборки; полное состояние всегда можно
восстановить из SQLite (снапшот в начале стрима).
"""
from __future__ import annotations

import asyncio
from typing import Any

Event = dict[str, Any]


class RunChannel:
    """Канал одного забега: очереди подписчиков + флаг закрытия."""

    def __init__(self) -> None:
        self.subscribers: set[asyncio.Queue[Event | None]] = set()
        self.closed = False

    def subscribe(self) -> asyncio.Queue[Event | None]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event | None]) -> None:
        self.subscribers.discard(queue)

    def publish(self, event: Event | None) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(event)


class RunHub:
    """Реестр каналов: открывается при старте забега, закрывается после вердикта."""

    def __init__(self) -> None:
        self._channels: dict[str, RunChannel] = {}

    def open(self, run_id: str) -> RunChannel:
        channel = RunChannel()
        self._channels[run_id] = channel
        return channel

    def get(self, run_id: str) -> RunChannel | None:
        """Канал, если забег открывался в этом процессе (иначе None)."""
        return self._channels.get(run_id)

    def is_active(self, run_id: str) -> bool:
        channel = self._channels.get(run_id)
        return bool(channel and not channel.closed)

    def closed(self, run_id: str) -> bool:
        channel = self._channels.get(run_id)
        return bool(channel and channel.closed)

    def publish(self, run_id: str, event: Event) -> None:
        channel = self._channels.get(run_id)
        if channel is None:
            channel = self.open(run_id)  # события в никуда: подписчиков ещё нет
        channel.publish(event)

    def close(self, run_id: str) -> None:
        channel = self._channels.get(run_id)
        if channel is None:
            channel = self.open(run_id)
        channel.closed = True
        channel.publish(None)  # sentinel: «поток закончен»


hub = RunHub()
