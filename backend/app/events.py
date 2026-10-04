"""In-process event broker behind GET /api/events (Server-Sent Events).

Each SSE connection subscribes one asyncio.Queue; publish() fans an event out to all of them
without ever waiting. A dashboard that stops reading (a stalled tab, a tunnel that buffers)
gets its events dropped once its queue is full instead of slowing everyone else down; the
dashboard re-fetches a full snapshot whenever it reconnects, so a dropped event is not lost data.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

QUEUE_SIZE = 200  # per subscriber; storm mode produces ~1 event/s, so this is minutes of slack


class Broker:
    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        # queue -> the event loop it belongs to (asyncio queues must be fed from their own loop)
        self._subscribers: dict[asyncio.Queue, asyncio.AbstractEventLoop] = {}

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self) -> asyncio.Queue:
        """New queue that receives every event published from now on. Call from inside the event loop."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers[queue] = asyncio.get_running_loop()
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.pop(queue, None)

    def publish(self, event: dict) -> None:
        """Hand the event to every subscriber. Never blocks and never raises."""
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None  # called from a worker thread
        for queue, loop in list(self._subscribers.items()):
            if loop.is_closed():  # its server/test loop is gone; nobody will ever read it
                self._subscribers.pop(queue, None)
                continue
            if loop is current:
                self._offer(queue, event)
            else:
                try:
                    loop.call_soon_threadsafe(self._offer, queue, event)
                except RuntimeError:
                    self._subscribers.pop(queue, None)

    @staticmethod
    def _offer(queue: asyncio.Queue, event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            log.warning("SSE subscriber is not keeping up; dropped a %s event", event.get("type"))


broker = Broker()
