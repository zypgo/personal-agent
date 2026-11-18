"""Small provider boundary shared by local/remote agent hosts.

Providers execute an existing native agent. They never acquire work or grant approvals.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol


@dataclass(frozen=True)
class NativeRequest:
    prompt: str
    cwd: Path
    profile_dir: Path
    run_id: str
    worker_secret_env: str | None = None
    trusted_prompt: str | None = None
    has_untrusted_inputs: bool = False


@dataclass(frozen=True)
class NativeCallbacks:
    progress: Callable[[str], Awaitable[None]]
    question: Callable[[str, str, list[str]], Awaitable[str | None]]
    state: Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class NativeResult:
    status: Literal["succeeded", "failed", "cancelled", "unknown"]
    text: str


class NativeProvider(Protocol):
    async def run(
        self, request: NativeRequest, callbacks: NativeCallbacks, cancel: asyncio.Event
    ) -> NativeResult:
        """Return a definite outcome only after the provider process has ended.

        Persist phase/process/session/turn changes with callbacks.state. Never restart a
        lost turn automatically. A denied or unavailable approval cannot become consent.
        """
        ...
