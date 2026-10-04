"""In-process event broker behind GET /api/events (Server-Sent Events).

Each SSE connection subscribes one asyncio.Queue; publish() fans an event out to all of them
without ever waiting. A dashboard that stops reading (a stalled tab, a tunnel that buffers)
must not slow everyone else down, but silently dropping its events would leave it showing stale
pins (a report stuck at "processing..."). So when its queue overflows, the subscriber is cut off:
its backlog is thrown away and replaced by one RESYNC marker, which ends that SSE stream. The
browser's EventSource reconnects on its own, and the dashboard loads a full snapshot on every
(re)connect, so nothing is lost and the log gets one line instead of one per dropped event.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

QUEUE_SIZE = 200  # per subscriber; a fast storm makes ~5 events/s, so this is ~40 s of slack

# Put in a cut-off subscriber's queue instead of further events: "stop streaming, let the client
# reconnect". Internal only; the SSE endpoint never sends it.
RESYNC: dict = {"type": "_resync"}


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

    def _offer(self, queue: asyncio.Queue, event: dict) -> None:
        if queue not in self._subscribers:
            return  # unsubscribed (or cut off) after this event was scheduled for it
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Too far behind to catch up event by event: cut it off and make it reload instead.
            self._subscribers.pop(queue, None)
            while not queue.empty():
                queue.get_nowait()
            queue.put_nowait(RESYNC)
            log.warning("a live dashboard fell %d events behind; ending its stream so it reconnects and reloads",
                        self._queue_size)


broker = Broker()
