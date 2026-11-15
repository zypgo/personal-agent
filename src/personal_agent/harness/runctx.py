"""The per-run context object handed to every tool (via the SDK's `RunContextWrapper`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from personal_agent.core.events import Trust

if TYPE_CHECKING:
    from personal_agent.app import App

PROFILES = ("full", "scheduled", "untrusted", "dream")


@dataclass(frozen=True)
class Origin:
    """Where a run's answer should go."""

    channel: str
    recipient: str | None = None
    interactive: bool = True
    """A person is waiting for the reply (their final answer is delivered automatically)."""


@dataclass
class RunContext:
    app: App
    run_id: str
    session_key: str
    profile: str
    origin: Origin
    trust: Trust = Trust.OWNER
    tainted: bool = False
    taint_reasons: list[str] = field(default_factory=list)
    queued_messages: list[int] = field(default_factory=list)

    def taint(self, reason: str) -> None:
        """Record that this run has read content the owner did not write.

        From here on, sensitive tools need the owner's approval (policy, iteration 2)."""
        if not self.tainted:
            self.tainted = True
            self.app.runs.mark_tainted(self.run_id)
        if reason not in self.taint_reasons:
            self.taint_reasons.append(reason)
