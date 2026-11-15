"""Dispatcher: delivers outbox messages through channel adapters.

Rules (the "outbox" primitive):
- A message counts as sent only after the channel confirms delivery.
- Only then are its `on_sent` commits (bookmarks, ledgers) applied, so state never claims that
  something was delivered when it was not.
- `ChannelError` = definitely not delivered: retry with backoff (or give up if permanent).
  Any other exception = unknown outcome: never resend a normal message blindly (`maybe_sent`);
  an urgent one is retried because a duplicate alert beats a lost one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from personal_agent.channels.base import Channel, ChannelError
from personal_agent.clock import in_quiet_hours, parse_quiet_hours, quiet_hours_end
from personal_agent.core.outbox import OutMessage
from personal_agent.memory.store import Actor

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

RETRY_BACKOFF_S = (5.0, 30.0, 120.0, 600.0)
MAX_ATTEMPTS = 5
_FAILURE_PREFIXES = ("Error", "No replacement")


@dataclass
class DeliveryReport:
    sent: list[int] = field(default_factory=list)
    failed: list[int] = field(default_factory=list)
    retrying: list[int] = field(default_factory=list)
    unknown: list[int] = field(default_factory=list)
    held: list[int] = field(default_factory=list)
    """Postponed by quiet hours or the hourly limit; they go out later."""


class Dispatcher:
    def __init__(self, app: App, channels: dict[str, Channel]) -> None:
        self.app = app
        self.channels = channels

    async def deliver_once(self, *, limit: int = 20) -> DeliveryReport:
        report = DeliveryReport()
        outbox = self.app.outbox
        for message in outbox.claim_due(channels=set(self.channels), limit=limit):
            channel = self.channels[message.channel]
            until = self._not_before(message)
            if until is not None:
                outbox.hold(message.id, until)
                report.held.append(message.id)
                continue
            try:
                await channel.send(message)
            except ChannelError as exc:
                retry_in = (
                    None if exc.permanent else (exc.retry_after or self._backoff(message.attempts))
                )
                status = outbox.mark_failed(
                    message.id, str(exc), retry_in=retry_in, max_attempts=MAX_ATTEMPTS
                )
                (report.retrying if status == "pending" else report.failed).append(message.id)
            except Exception as exc:
                log.exception(
                    "channel %s raised while sending message %s", message.channel, message.id
                )
                if message.urgency == "urgent":
                    outbox.mark_failed(
                        message.id,
                        f"unknown outcome: {exc}",
                        retry_in=self._backoff(message.attempts),
                    )
                    report.retrying.append(message.id)
                else:
                    outbox.mark_maybe_sent(message.id, f"unknown outcome: {exc}")
                    report.unknown.append(message.id)
            else:
                outbox.mark_sent(message.id)
                report.sent.append(message.id)
                self._record(message)
                self._commit(message)
        return report

    def _record(self, message: OutMessage) -> None:
        """Add the delivered message to the owner's history. Never allowed to break delivery."""
        try:
            self.app.transcript.add_out(message)
        except Exception:
            log.warning("could not record message %s in the transcript", message.id, exc_info=True)

    def _not_before(self, message: OutMessage) -> float | None:
        """When this message may go out if it must wait, or None to send it now.

        Quiet hours and the hourly limit protect the owner's attention from *background* work.
        Answers to something the owner just said, and urgent alerts, always go straight through.
        """
        if message.urgency == "urgent" or message.payload.get("interactive"):
            return None
        app = self.app
        now = app.clock.now()
        notify = app.config.notify
        if notify.quiet_hours:
            window = parse_quiet_hours(notify.quiet_hours)
            if in_quiet_hours(now, app.tz, window):
                return quiet_hours_end(now, app.tz, window)
        recent = app.outbox.background_sends_since(message.channel, now - 3600)
        if len(recent) >= notify.max_per_hour:
            return recent[len(recent) - notify.max_per_hour] + 3600
        return None

    @staticmethod
    def _backoff(attempts: int) -> float:
        return RETRY_BACKOFF_S[min(max(attempts, 1) - 1, len(RETRY_BACKOFF_S) - 1)]

    def _commit(self, message: OutMessage) -> None:
        """Apply the memory commands the agent attached to this message, now that it is delivered."""
        for command in message.on_sent:
            result = self.app.memory.execute(command, actor=Actor.AGENT, run_id=message.run_id)
            if result.startswith(_FAILURE_PREFIXES):
                log.warning("on_sent commit for message %s failed: %s", message.id, result)
