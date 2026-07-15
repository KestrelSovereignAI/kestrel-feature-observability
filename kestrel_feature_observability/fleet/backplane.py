"""Live-stream fan-out behind a pub/sub interface.

The stream endpoint never talks to subscribers directly — it publishes ingested
events to a :class:`PubSub` backplane and each connected client subscribes. This
keeps the transport swappable:

* :class:`InProcessPubSub` — the v1 default. A single-process fan-out with a
  bounded replay ring so a reconnecting client can resume from its
  ``Last-Event-ID``.
* :class:`PostgresPubSub` — a multi-instance seam over Postgres ``LISTEN`` /
  ``NOTIFY``. Left as a documented stub for v1; ``create_pubsub("postgres", …)``
  is where a later phase wires it in.

Each published event is assigned a monotonic **stream id** (stringified int).
Subscribers resume by passing the last id they saw; the backplane replays any
buffered events with a greater id, then streams live.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from collections import deque
from typing import Any, AsyncIterator, Deque, Optional, Set, Tuple

# A delivered frame: (stream_id, event_payload).
Frame = Tuple[int, dict]


class Subscription:
    """A single live subscriber.

    Async-iterates ``(stream_id, event)`` frames. Created via
    :meth:`PubSub.subscribe`; always close it (``aclose`` / ``async with``) so
    the backplane drops its queue.
    """

    def __init__(self, pubsub: "InProcessPubSub", backlog: list[Frame]) -> None:
        self._pubsub = pubsub
        self._queue: "asyncio.Queue[Optional[Frame]]" = asyncio.Queue()
        # Seed replayed backlog (resume-from-Last-Event-ID) ahead of live frames.
        for frame in backlog:
            self._queue.put_nowait(frame)
        self._closed = False

    def _offer(self, frame: Frame) -> None:
        if not self._closed:
            self._queue.put_nowait(frame)

    async def __aiter__(self) -> AsyncIterator[Frame]:
        while True:
            frame = await self._queue.get()
            if frame is None:  # close sentinel
                return
            yield frame

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pubsub._drop(self)
        self._queue.put_nowait(None)

    async def __aenter__(self) -> "Subscription":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()


class PubSub(ABC):
    """Pub/sub backplane contract for the observability live stream."""

    @abstractmethod
    async def start(self) -> None:
        """Start the backplane (open connections, background listeners)."""

    @abstractmethod
    async def stop(self) -> None:
        """Stop the backplane and disconnect all subscribers."""

    @abstractmethod
    async def publish(self, event: dict) -> int:
        """Publish ``event`` to all subscribers; return its stream id."""

    @abstractmethod
    def subscribe(self, last_event_id: Optional[int] = None) -> Subscription:
        """Subscribe, replaying buffered frames after ``last_event_id``."""


class InProcessPubSub(PubSub):
    """Single-process fan-out with a bounded replay ring.

    Sufficient for a single host instance. ``replay_size`` bounds how far a
    reconnecting client can rewind via ``Last-Event-ID``.
    """

    def __init__(self, replay_size: int = 256) -> None:
        self._subscribers: Set[Subscription] = set()
        self._ring: Deque[Frame] = deque(maxlen=replay_size)
        self._next_id = 1
        self._lock = asyncio.Lock()
        self._started = False

    async def start(self) -> None:
        self._started = True

    async def stop(self) -> None:
        self._started = False
        for sub in list(self._subscribers):
            await sub.aclose()
        self._subscribers.clear()
        self._ring.clear()

    async def publish(self, event: dict) -> int:
        async with self._lock:
            stream_id = self._next_id
            self._next_id += 1
            frame: Frame = (stream_id, event)
            self._ring.append(frame)
            for sub in list(self._subscribers):
                sub._offer(frame)
        return stream_id

    def subscribe(self, last_event_id: Optional[int] = None) -> Subscription:
        backlog: list[Frame] = []
        if last_event_id is not None:
            backlog = [f for f in self._ring if f[0] > last_event_id]
        sub = Subscription(self, backlog)
        self._subscribers.add(sub)
        return sub

    def _drop(self, sub: Subscription) -> None:
        self._subscribers.discard(sub)


class PostgresPubSub(PubSub):
    """Multi-instance seam over Postgres ``LISTEN`` / ``NOTIFY``.

    Documented stub for v1: the interface is fixed so a later phase can wire a
    real ``LISTEN``/``NOTIFY`` transport (fan-out across host instances sharing
    one Postgres) without touching the stream endpoint. Until then it degrades
    to an in-process ring so a single instance still works.
    """

    def __init__(self, dsn: str, channel: str = "observability_events", replay_size: int = 256) -> None:
        self._dsn = dsn
        self._channel = channel
        self._local = InProcessPubSub(replay_size=replay_size)

    async def start(self) -> None:  # pragma: no cover - seam
        await self._local.start()

    async def stop(self) -> None:  # pragma: no cover - seam
        await self._local.stop()

    async def publish(self, event: dict) -> int:  # pragma: no cover - seam
        # A real implementation also issues `NOTIFY <channel>, <payload>` so
        # peer instances re-publish onto their own local rings.
        return await self._local.publish(event)

    def subscribe(self, last_event_id: Optional[int] = None) -> Subscription:  # pragma: no cover - seam
        return self._local.subscribe(last_event_id)


def create_pubsub(backend: str = "memory", *, dsn: Optional[str] = None, replay_size: int = 256) -> PubSub:
    """Construct a backplane for ``backend`` (``"memory"`` or ``"postgres"``)."""
    if backend == "postgres":
        if not dsn:
            raise ValueError("postgres backplane requires a dsn")
        return PostgresPubSub(dsn, replay_size=replay_size)
    if backend == "memory":
        return InProcessPubSub(replay_size=replay_size)
    raise ValueError(f"unknown backplane backend: {backend!r}")


__all__ = [
    "PubSub",
    "Subscription",
    "InProcessPubSub",
    "PostgresPubSub",
    "create_pubsub",
    "Frame",
]
