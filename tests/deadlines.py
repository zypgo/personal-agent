"""Expire real asyncio deadlines at a chosen stage, without racing slow CI setup."""

from __future__ import annotations

import asyncio


class Deadlines:
    def __init__(self, monkeypatch, module):
        self.created = []
        self.seconds = []
        # Replace only the tested module's reference, not asyncio.timeout process-wide: SDK
        # tools, worker leases and the test's own watchdog keep their ordinary timing.
        monkeypatch.setattr(module, "asyncio", self)

    def __getattr__(self, name):
        return getattr(asyncio, name)

    def timeout(self, seconds):
        self.seconds.append(seconds)
        deadline = asyncio.timeout(None)
        self.created.append(deadline)
        return deadline

    def expire(self):
        self.created[-1].reschedule(asyncio.get_running_loop().time())
