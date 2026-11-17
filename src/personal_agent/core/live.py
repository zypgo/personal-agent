"""A tiny in-process bus for "something just happened" nudges to the apps that are open.

The database is authoritative for committed messages and work state. Nudges let an open page
or typing indicator notice changes immediately. Bounded answer previews are intentionally
ephemeral: a subscriber replaces them from the current snapshot and replays durable history
from the database. Losing a preview cannot lose a committed answer.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import Counter
from collections.abc import Iterator
from typing import Any

from personal_agent.security.redact import known_secret_values, redact
from personal_agent.security.sanitize import scrub_surrogates, strip_invisible

log = logging.getLogger(__name__)

Event = dict[str, Any]

MAX_DRAFT_CHARS = 32_000
MAX_DRAFTS = 16


def preview_text(text: str, *, settled: bool = False) -> str:
    """Scrub a preview, including a known credential split across model chunks.

    Withhold a suffix that could still become a secret; scrubbing individual deltas would
    expose a token one piece at a time. The final message still comes from the durable outbox.
    """
    # Normalize before comparing suffixes: a hidden bidi/control character must not let a
    # partial credential through just because its raw bytes did not match the known prefix.
    text = scrub_surrogates(strip_invisible(text))
    # Multiline credentials may themselves contain CRLF or hidden characters. Compare both
    # sides in the same form or normalization could erase the only full-secret match.
    known = [scrub_surrogates(strip_invisible(value)) for value in known_secret_values()]
    end = len(text)
    if not settled:
        for secret in known:
            for size in range(min(len(secret) - 1, len(text)), 0, -1):
                if text.endswith(secret[:size]):
                    end = min(end, len(text) - size)
                    break
    return redact(text[:end], known)


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
        self._drafts: dict[str, Event] = {}
        self._draft_text: dict[str, str] = {}
        self._draft_revision = 0
        self._draft_notified_at = 0.0

    def response_started(self, run_id: str) -> None:
        """Start or replace one model attempt's preview. Never used for background work."""
        if run_id not in self._drafts and len(self._drafts) >= MAX_DRAFTS:
            return
        self._draft_text[run_id] = ""
        self._draft_revision += 1
        self._drafts[run_id] = {"run_id": run_id, "sequence": self._draft_revision, "text": ""}
        self.publish({"type": "drafts"})

    def response_text(self, run_id: str, text: str, *, complete: bool = False) -> None:
        """Publish bounded *snapshots*, so a slow phone can miss deltas without losing words."""
        if run_id not in self._drafts:
            return
        previous = self._draft_text[run_id]
        raw = text if complete else previous + text
        truncated = len(raw) > MAX_DRAFT_CHARS
        raw = raw[:MAX_DRAFT_CHARS]
        self._draft_text[run_id] = raw
        self._draft_revision += 1
        self._drafts[run_id] = {
            "run_id": run_id,
            "sequence": self._draft_revision,
            "text": preview_text(raw, settled=complete and not truncated),
            "truncated": truncated,
        }
        now = time.monotonic()
        if complete or now - self._draft_notified_at >= 0.1:
            self._draft_notified_at = now
            self.publish({"type": "drafts"})

    def response_finished(self, run_id: str) -> None:
        """Remove previews on completion, approval, failure or cancellation."""
        self._draft_text.pop(run_id, None)
        if self._drafts.pop(run_id, None) is not None:
            self.publish({"type": "drafts"})

    def response_drafts(self) -> list[Event]:
        """Current safe previews for a newly connected browser. Not a durable transcript."""
        return [dict(value) for value in self._drafts.values()]

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
