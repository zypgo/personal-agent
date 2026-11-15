"""Worker: claims a batch from the inbox, runs the harness, settles the batch.

The settle step is where failures are attributed: a problem with the environment (no network,
rate limit, bad key) puts the events back without using up an attempt, while a problem that
belongs to the events or the code counts toward the poison-pill limit.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from personal_agent.core.events import Batch, Event, Kind, Trust
from personal_agent.harness.errors import Failure, classify
from personal_agent.harness.runner import Harness, RunOutcome

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

OnFailure = Literal["retry", "drop"]


@dataclass
class WorkResult:
    batch: Batch
    outcome: RunOutcome | None = None
    failure: Failure | None = None


class Worker:
    def __init__(
        self, app: App, harness: Harness | None = None, *, owner: str | None = None
    ) -> None:
        self.app = app
        self.harness = harness or Harness(app)
        self.owner = owner or f"worker-{os.getpid()}-{uuid.uuid4().hex[:6]}"

    async def run_once(
        self, *, session_key: str | None = None, on_failure: OnFailure = "retry"
    ) -> WorkResult | None:
        """Process the next ready batch. Returns None when there is nothing to do.

        `on_failure="drop"` is for an interactive caller that shows the error itself and does not
        want the message retried behind the owner's back.
        """
        app = self.app
        batch = app.inbox.claim(self.owner, session_key=session_key)
        self._tell_owner_about_dead(app.inbox.take_dead())
        if batch is None:
            return None

        beat = asyncio.create_task(self._keep_lease(batch))
        try:
            outcome = await self.harness.run(batch)
        except asyncio.CancelledError:
            app.inbox.release(batch)
            raise
        except Exception as exc:
            failure = classify(exc)
            if on_failure == "drop":
                app.inbox.discard(batch, failure.summary)
            else:
                app.inbox.fail(
                    batch,
                    failure.summary,
                    environmental=failure.environmental,
                    retry_in=failure.retry_in or None,
                )
                self._tell_owner_about_failure(batch, failure)
            return WorkResult(batch, failure=failure)
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat
        app.inbox.complete(batch)
        return WorkResult(batch, outcome=outcome)

    async def _keep_lease(self, batch: Batch) -> None:
        """Extend the lease while the run is alive so a slow run is not mistaken for a crash."""
        interval = max(self.app.inbox.lease_s / 3, 1.0)
        while True:
            await asyncio.sleep(interval)
            if not self.app.inbox.heartbeat(batch):
                log.warning("lost the lease on events %s while still running", batch.ids)
                return

    # ---- telling the owner -----------------------------------------------------------
    def _notice(
        self,
        *,
        channel: str,
        recipient: str | None,
        session_key: str,
        text: str,
        dedupe: str,
        interactive: bool = False,
    ) -> None:
        self.app.outbox.enqueue(
            session_key=session_key,
            channel=channel,
            recipient=recipient,
            text=text,
            kind="notice",
            dedupe_key=dedupe,
            extra={"interactive": interactive},
        )

    def _tell_owner_about_failure(self, batch: Batch, failure: Failure) -> None:
        """Say so once when something fails; otherwise the owner is left guessing.

        Always for an owner's message (someone is waiting). For background work only when the
        failure carries a `notice_key` (a paused budget), once per key and local day.
        """
        origin = self.harness.origin_for(batch)
        hint = failure.user_hint or "Something went wrong."
        if failure.notice_key:
            day = int(self.app.clock.now() // 86400)
            self._notice(
                channel=origin.channel,
                recipient=origin.recipient,
                session_key=batch.session_key,
                text=hint,
                dedupe=f"{failure.notice_key}:{day}",
                interactive=origin.interactive,
            )
            return
        if not origin.interactive:
            return
        self._notice(
            channel=origin.channel,
            recipient=origin.recipient,
            session_key=batch.session_key,
            text=f"{hint} I will retry automatically.",
            dedupe=f"fail:{batch.ids[0]}",
            interactive=True,
        )

    def _tell_owner_about_dead(self, dead: list[Event]) -> None:
        app = self.app
        day = int(app.clock.now() // 86400)
        for event in dead:
            if event.kind == Kind.APPROVAL_RESPONSE:
                self._abandon_resume(event)
            elif event.kind == Kind.MESSAGE and event.trust is Trust.OWNER:
                self._notice(
                    channel=event.source,
                    recipient=event.payload.get("recipient"),
                    session_key=event.session_key,
                    text="I could not process your message and gave up on it. Please send it again.",
                    dedupe=f"dead:{event.id}",
                    interactive=True,
                )
            else:
                self._notice(
                    channel=app.config.notify.channel,
                    recipient=None,
                    session_key=event.session_key,
                    text=f"An event from '{event.source}' ({event.kind}) failed repeatedly and was dropped.",
                    dedupe=f"dead:{event.source}:{day}",
                )

    def _abandon_resume(self, event: Event) -> None:
        """A suspended run that cannot be resumed after repeated attempts: give up on it, tell
        the owner, and leave the session consistent (its pending calls get an explicit result)."""
        app = self.app
        run_id = str(event.payload.get("run_id") or "")
        saved = app.approvals.load_run(run_id)
        if saved is None:
            return
        app.approvals.drop_run(run_id)
        app.runs.finish(run_id, "failed", error="could not resume after the approval")
        run = app.runs.get(run_id)
        channel = app.config.notify.channel
        recipient = None
        origin = saved.meta.get("origin") or {}
        channel = origin.get("channel") or channel
        recipient = origin.get("recipient")
        self._notice(
            channel=channel,
            recipient=recipient,
            session_key=event.session_key,
            text=(
                "I could not continue a task after you answered its approval request, "
                f"so I dropped it (run {run.id if run else run_id}). Ask me again if you still want it."
            ),
            dedupe=f"dead-resume:{run_id}",
        )
