"""Event model. Everything that can wake the agent is an `Event`."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Trust(StrEnum):
    """Who is behind an event. Decided by the channel adapter, never by the content."""

    OWNER = "owner"
    UNTRUSTED = "untrusted"


class Kind(StrEnum):
    MESSAGE = "message"  # payload: {"text": str, "attachments"?: [...], "reply_to"?: {...}}
    TIMER = "timer"  # payload: {"timer_id": int, "name": str, "prompt": str, "due_at": float}
    APPROVAL_RESPONSE = (
        "approval_response"  # payload: {"approval_id": int, "decision": "approve"|"reject"}
    )
    WEBHOOK = "webhook"  # payload: {"name": str, "body": Any}
    SYSTEM = "system"  # payload: {"name": str, ...}


@dataclass(frozen=True)
class NewEvent:
    source: str
    kind: str
    session_key: str
    payload: dict[str, Any]
    external_id: str | None = None
    """Idempotency key within `source`: re-submitting the same one is a no-op."""
    trust: Trust = Trust.OWNER
    delay_s: float = 0.0


@dataclass(frozen=True)
class Event:
    id: int
    source: str
    kind: str
    session_key: str
    payload: dict[str, Any]
    external_id: str | None
    trust: Trust
    attempts: int
    created_at: float

    @property
    def text(self) -> str:
        """Primary text of a message-like event ("" if none)."""
        value = self.payload.get("text")
        return value if isinstance(value, str) else ""


@dataclass
class Batch:
    """Events of one session claimed together under a lease, all of one trust level."""

    session_key: str
    owner: str
    events: list[Event]
    lease_expires_at: float
    ids: list[int] = field(init=False)
    lost: bool = field(default=False, init=False)
    """Set by the worker when the lease was lost while the run was going: the events are
    somebody else's now, and this batch must not be settled."""

    def __post_init__(self) -> None:
        self.ids = [e.id for e in self.events]

    @property
    def trusted(self) -> bool:
        return all(e.trust is Trust.OWNER for e in self.events)

    @property
    def kinds(self) -> list[str]:
        seen: list[str] = []
        for e in self.events:
            if e.kind not in seen:
                seen.append(e.kind)
        return seen


@dataclass(frozen=True)
class AddResult:
    id: int
    duplicate: bool
