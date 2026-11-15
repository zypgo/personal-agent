"""A tiny in-process bus for "something just happened" nudges to the apps that are open.

Nothing here is a source of truth. The database holds every message and every state; the bus only
lets an open web page or a chat typing indicator learn about a change right away instead of at
its next poll. A subscriber that misses an event loses nothing: it re-reads the database.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import Counter
from collections.abc import Iterator
from typing import Any

log = logging.getLogger(__name__)

Event = dict[str, Any]


class Subscription:
    """One listener. Bounded: a listener that stops reading loses its oldest nudges, not memory."""

    def __init__(self, bus: LiveBus, loop: asyncio.AbstractEventLoop, maxsize: int) -> None:
        self._bus = bus
        self._loop = loop
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize)

    def _deliver(self, event: Event) -> None:
        if self._queue.full():
            self._queue.get_nowait()
        self._queue.put_nowait(event)

    async def next(self, wait_s: float) -> Event | None:
        """The next event, or None if nothing came within `wait_s` seconds."""
        try:
            async with asyncio.timeout(wait_s):
                return await self._queue.get()
        except TimeoutError:
            return None

    def drain(self) -> list[Event]:
        events: list[Event] = []
        while not self._queue.empty():
            events.append(self._queue.get_nowait())
        return events

    def close(self) -> None:
        self._bus._remove(self)

    def __enter__(self) -> Subscription:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LiveBus:
    def __init__(self) -> None:
        self._subscriptions: list[Subscription] = []
        self._busy: Counter[str] = Counter()

    def subscribe(self, maxsize: int = 64) -> Subscription:
        """Listen for events. Call it from inside the event loop that will read them."""
        sub = Subscription(self, asyncio.get_running_loop(), maxsize)
        self._subscriptions.append(sub)
        return sub

    def _remove(self, sub: Subscription) -> None:
        try:
            self._subscriptions.remove(sub)
        except ValueError:
            pass

    @property
    def listeners(self) -> int:
        return len(self._subscriptions)

    def publish(self, event: Event) -> None:
        """Tell every listener. Safe from any thread; never blocks and never raises."""
        for sub in list(self._subscriptions):
            try:
                sub._loop.call_soon_threadsafe(sub._deliver, event)
            except RuntimeError:  # that loop has ended: nobody is listening any more
                self._remove(sub)

    # ---- "the agent is working" -------------------------------------------------------
    @property
    def busy(self) -> bool:
        return sum(self._busy.values()) > 0

    @contextlib.contextmanager
    def working(self, session: str, *, channel: str | None = None) -> Iterator[None]:
        """Mark a conversation as being worked on; listeners learn when work starts and when the
        last of it ends (several conversations can run at once). `channel` is where the owner
        asked, if they did: a chat app shows "typing..." only for its own conversations."""
        self._busy[session] += 1
        self.publish({"type": "activity", "working": True, "channel": channel, "tool": None})
        try:
            yield
        finally:
            self._busy[session] -= 1
            if self._busy[session] <= 0:
                del self._busy[session]
            self.publish(
                {"type": "activity", "working": self.busy, "channel": channel, "tool": None}
            )

    def tool_started(self, name: str) -> None:
        """Which tool the agent just began using (names only: arguments can hold private data)."""
        self.publish({"type": "activity", "working": True, "tool": name})
