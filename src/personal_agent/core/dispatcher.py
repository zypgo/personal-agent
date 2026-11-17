"""Dispatcher: delivers outbox messages through channel adapters.

Rules (the "outbox" primitive):
- A message counts as sent only after the channel confirms delivery.
- Only then are its `on_sent` commits (bookmarks, ledgers) applied, so state never claims that
  something was delivered when it was not.
- `ChannelError` = definitely not delivered: retry with backoff (or give up if permanent).
  Any other exception = unknown outcome: never resend a background message blindly
  (`maybe_sent`); one that must arrive (an alert, an answer to the owner, a request for approval)
  is retried because a duplicate beats a lost one.
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
            actor = message.payload.get("principal_id") or message.payload.get("actor_id")
            if actor is not None:
                from personal_agent.collaboration.models import AccessDenied

                try:
                    self.app.require_actor(
                        str(actor),
                        version=message.payload.get("membership_version"),
                        principal_version=message.payload.get("principal_version"),
                    )
                except AccessDenied:
                    self.app.db.execute(
                        "UPDATE outbox SET status='cancelled', last_error='authorization changed' WHERE id=? AND status='sending'",
                        (message.id,),
                    )
                    report.failed.append(message.id)
                    continue
            if (
                self.app.space_id != "personal-owner"
                and message.channel not in ("web", "cli")
                and not message.recipient
            ):
                self.app.db.execute(
                    "UPDATE outbox SET status='cancelled', last_error='recipient required' WHERE id=? AND status='sending'",
                    (message.id,),
                )
                report.failed.append(message.id)
                continue
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
                if message.must_arrive:
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
                # Delivery, transcript and desired memory contents commit as one unit. Memory
                # files are projected after COMMIT; a crash replays those bytes idempotently.
                with self.app.db.tx():
                    outbox.mark_sent(message.id)
                    self._record(message)
                    self._commit(message)
                report.sent.append(message.id)
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
        """Apply the memory commands the agent attached to this message, now that it is delivered.
        Never raises: the message is delivered whatever becomes of its bookmarks."""
        if not message.on_sent:
            return
        actor = message.payload.get("principal_id") or message.payload.get("actor_id")
        if actor is not None:
            from personal_agent.collaboration.models import AccessDenied

            try:
                self.app.require_actor(
                    str(actor),
                    version=message.payload.get("membership_version"),
                    principal_version=message.payload.get("principal_version"),
                )
            except AccessDenied:
                # Delivery already happened and remains recorded as sent. A lost permission
                # during the network await must not authorize subsequent memory mutations.
                log.info("on_sent commits of message %s skipped: authorization changed", message.id)
                return
        if self._from_untrusted_run(message):
            # `send_message` refuses these now; a message queued before that, or by anything else,
            # is not allowed to write memory on behalf of a run the owner's words did not start.
            log.warning("on_sent commits of message %s skipped: untrusted run", message.id)
            return
        for command in message.on_sent:
            try:
                result = self.app.memory.execute(command, actor=Actor.AGENT, run_id=message.run_id)
            except Exception:
                log.warning("on_sent commit for message %s failed", message.id, exc_info=True)
                continue
            if result.startswith(_FAILURE_PREFIXES):
                log.warning("on_sent commit for message %s failed: %s", message.id, result)

    def _from_untrusted_run(self, message: OutMessage) -> bool:
        from personal_agent.tools.base import UNTRUSTED  # the model SDK; only needed when running

        run = self.app.runs.get(message.run_id) if message.run_id else None
        return run is not None and run.profile == UNTRUSTED
