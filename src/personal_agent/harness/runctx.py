"""The per-run context object handed to every tool (via the SDK's `RunContextWrapper`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from personal_agent.core.events import Trust

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.security.policy import Decision

PROFILES = ("full", "scheduled", "untrusted", "dream")


@dataclass(frozen=True)
class Origin:
    """Where a run's answer should go."""

    channel: str
    recipient: str | None = None
    interactive: bool = True
    """A person is waiting for the reply (their final answer is delivered automatically)."""

    def to_json(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "recipient": self.recipient,
            "interactive": self.interactive,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Origin:
        return cls(
            channel=str(data.get("channel") or "cli"),
            recipient=data.get("recipient"),
            interactive=bool(data.get("interactive", False)),
        )


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
    owner_request: str = ""
    """What the owner last asked (trusted text), shown to the reviewer in `auto` mode."""
    decisions: dict[str, Decision] = field(default_factory=dict)
    """Policy decisions by tool call id, so a call is judged once."""

    def taint(self, reason: str) -> None:
        """Record that this run has read content the owner did not write.

        From here on, tools that change the world need the owner's approval (see `Policy`)."""
        if not self.tainted:
            self.tainted = True
            self.app.runs.mark_tainted(self.run_id)
        if reason not in self.taint_reasons:
            self.taint_reasons.append(reason)

    # ---- suspension --------------------------------------------------------------------
    def to_meta(self) -> dict[str, Any]:
        """What must survive while the run waits for the owner (small; no app handles)."""
        return {
            "profile": self.profile,
            "origin": self.origin.to_json(),
            "trust": self.trust.value,
            "tainted": self.tainted,
            "taint_reasons": list(self.taint_reasons),
            "queued_messages": list(self.queued_messages),
            "owner_request": self.owner_request,
        }

    @classmethod
    def from_meta(cls, app: App, run_id: str, session_key: str, meta: dict[str, Any]) -> RunContext:
        return cls(
            app=app,
            run_id=run_id,
            session_key=session_key,
            profile=str(meta.get("profile") or "full"),
            origin=Origin.from_json(meta.get("origin") or {}),
            trust=Trust(meta.get("trust") or "owner"),
            tainted=bool(meta.get("tainted", False)),
            taint_reasons=list(meta.get("taint_reasons") or []),
            queued_messages=[int(i) for i in meta.get("queued_messages") or []],
            owner_request=str(meta.get("owner_request") or ""),
        )
