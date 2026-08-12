"""In-memory SSE hub.

One bounded queue per subscriber plus a lock; no broker. Events are advisory —
a reconnecting client re-reads state from `GET /api/channels` rather than
replaying, so nothing here is persisted and a subscriber that cannot keep up is
dropped instead of buffered. One stuck browser tab must not grow the backend.

Event names are the frozen set in docs/contracts/rest-api.md.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator

LOG = logging.getLogger("ambient.events")

CHANNEL_STATUS = "channel.status"
CHANNEL_PROGRESS = "channel.progress"
CHANNEL_TRACK = "channel.track"
CHANNEL_SLIDE = "channel.slide"
CHANNEL_VISUALISATION = "channel.visualisation"
WATCHDOG_EVENT = "watchdog.event"
CAPACITY_WARNING = "capacity.warning"
JOB_PROGRESS = "job.progress"

EVENT_NAMES = frozenset(
    {
        CHANNEL_STATUS,
        CHANNEL_PROGRESS,
        CHANNEL_TRACK,
        CHANNEL_SLIDE,
        CHANNEL_VISUALISATION,
        WATCHDOG_EVENT,
        CAPACITY_WARNING,
        JOB_PROGRESS,
    }
)

# `-progress` emits far faster than any UI needs.
PROGRESS_MIN_INTERVAL = 1.0
DEFAULT_QUEUE_SIZE = 64
DEFAULT_KEEPALIVE = 15.0


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass(frozen=True)
class Event:
    name: str
    channel: str | None
    at: str
    data: dict[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"channel": self.channel, "at": self.at}
        for key, value in self.data.items():
            if key not in ("channel", "at"):
                body[key] = value
        return body

    def sse(self) -> str:
        body = json.dumps(self.payload(), separators=(",", ":"), default=str)
        return f"event: {self.name}\ndata: {body}\n\n"


class Subscriber:
    """One connected client. `closed` is set when the hub drops it."""

    def __init__(self, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.closed = asyncio.Event()
        self.dropped = False

    def close(self, *, dropped: bool = False) -> None:
        self.dropped = self.dropped or dropped
        self.closed.set()


class EventHub:
    def __init__(
        self,
        *,
        queue_size: int = DEFAULT_QUEUE_SIZE,
        progress_interval: float = PROGRESS_MIN_INTERVAL,
    ) -> None:
        self._subscribers: set[Subscriber] = set()
        self._lock = asyncio.Lock()
        self._queue_size = queue_size
        self._progress_interval = progress_interval
        self._last_progress: dict[str, float] = {}
        self.dropped_subscribers = 0
        self.published = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def _throttled(self, channel: str | None, now: float) -> bool:
        key = channel or ""
        previous = self._last_progress.get(key)
        if previous is not None and now - previous < self._progress_interval:
            return True
        self._last_progress[key] = now
        return False

    async def publish(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        *,
        channel: str | None = None,
    ) -> bool:
        """Fan out one event. Returns False when it was throttled away."""
        if name not in EVENT_NAMES:
            raise ValueError(f"unknown event name {name!r}")
        if name == CHANNEL_PROGRESS and self._throttled(channel, asyncio.get_running_loop().time()):
            return False

        event = Event(name=name, channel=channel, at=utc_now_iso(), data=dict(data or {}))
        frame = event.sse()
        async with self._lock:
            slow: list[Subscriber] = []
            for subscriber in self._subscribers:
                try:
                    subscriber.queue.put_nowait(frame)
                except asyncio.QueueFull:
                    slow.append(subscriber)
            for subscriber in slow:
                self._subscribers.discard(subscriber)
                subscriber.close(dropped=True)
                self.dropped_subscribers += 1
                LOG.warning("dropped a slow SSE subscriber: queue full at %d", self._queue_size)
        self.published += 1
        return True

    @asynccontextmanager
    async def subscribe(self) -> AsyncIterator[Subscriber]:
        subscriber = Subscriber(self._queue_size)
        async with self._lock:
            self._subscribers.add(subscriber)
        try:
            yield subscriber
        finally:
            async with self._lock:
                self._subscribers.discard(subscriber)
            subscriber.close()

    async def stream(
        self, subscriber: Subscriber, *, keepalive: float = DEFAULT_KEEPALIVE
    ) -> AsyncIterator[str]:
        """Frames for one client, with comment keepalives so proxies hold on."""
        yield ": connected\n\n"
        pending: asyncio.Task[str] | None = None
        closed = asyncio.ensure_future(subscriber.closed.wait())
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(subscriber.queue.get())
                done, _ = await asyncio.wait(
                    {pending, closed},
                    timeout=keepalive,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if pending in done:
                    yield pending.result()
                    pending = None
                    continue
                if closed in done:
                    return
                yield ": keepalive\n\n"
        finally:
            for task in (pending, closed):
                if task is not None and not task.done():
                    task.cancel()

    async def close(self) -> None:
        async with self._lock:
            for subscriber in list(self._subscribers):
                subscriber.close()
            self._subscribers.clear()
