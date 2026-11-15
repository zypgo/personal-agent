"""The per-run context object handed to every tool (via the SDK's `RunContextWrapper`)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from personal_agent.core.events import Trust
from personal_agent.tools.urls import keys_of, url_key

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.security.policy import Decision

PROFILES = ("full", "scheduled", "untrusted", "dream")
MAX_KNOWN_URLS = 4000


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
    work_id: str = ""
    """Names the work this run does (the events that woke it), and stays the same when the work is
    tried again after a crash. What a run queues is deduplicated by it, so a retry does not send
    again what the interrupted attempt had already queued. Defaults to the run itself."""
    owner_request: str = ""
    """What the owner last asked (trusted text), shown to the reviewer in `auto` mode."""
    cost_cap_usd: float | None = None
    """A lower spending limit than `budget.per_run_usd` for work that should stay cheap."""
    decisions: dict[str, Decision] = field(default_factory=dict)
    """Policy decisions by tool call id, so a call is judged once."""
    known_urls: dict[str, None] = field(default_factory=dict)
    """Fingerprints of web addresses this run has legitimately seen (the owner wrote them, a page
    or search result linked to them, or a request to them went ahead). After the run has read
    content the owner did not write, an address outside this set needs the owner's approval: an
    address can carry data out. Insertion-ordered so the oldest can be dropped."""

    def __post_init__(self) -> None:
        self.work_id = self.work_id or f"run:{self.run_id}"

    def know_urls(self, urls: Iterable[str]) -> None:
        for key in keys_of(urls):
            self.known_urls.setdefault(key)
        while len(self.known_urls) > MAX_KNOWN_URLS:
            del self.known_urls[next(iter(self.known_urls))]

    def knows_url(self, url: str) -> bool:
        key = url_key(url)
        return key is not None and key in self.known_urls

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
            "known_urls": list(self.known_urls),
            "work_id": self.work_id,
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
            known_urls=dict.fromkeys(str(k) for k in meta.get("known_urls") or []),
            work_id=str(meta.get("work_id") or ""),
        )
