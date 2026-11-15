"""The harness: wake up, rebuild context from durable state, run the agent loop, stop.

It keeps no state between runs. Everything it needs is read at the start of a run (preferences
fresh, memory index, session history), and everything it produces goes to durable stores
(session log, outbox, run record). A crash at any point leaves a log that the next run can
continue from.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from agents import Agent, RunConfig, RunContextWrapper, RunHooks, Runner
from agents.exceptions import MaxTurnsExceeded

from personal_agent.core.events import Batch, Kind, Trust
from personal_agent.core.sessions import SdkSession
from personal_agent.harness.errors import PreferencesProblem, classify
from personal_agent.harness.instructions import (
    build_instructions,
    context_block,
    describe_trigger,
    event_to_item,
)
from personal_agent.harness.models import model_settings
from personal_agent.harness.pricing import cost_usd
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.memory.store import PreferencesUnavailable

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

RESUME_NOTE = (
    "[The previous attempt at this wake-up was interrupted. Continue from where the conversation "
    "stands; do not repeat actions that already completed.]"
)
DEFAULT_NOTIFY_CHANNEL = "cli"


@dataclass
class RunOutcome:
    run_id: str
    status: str  # completed | silent | failed
    final_text: str = ""
    queued_message_ids: list[int] = field(default_factory=list)
    error: str | None = None


class RunAccounting(RunHooks[RunContext]):
    """Writes usage and tool-call counts after every model call and tool call, so that cost is
    recorded even if the run later crashes, and budget guards can watch spend as it happens."""

    def __init__(self, app: App, model_name: str) -> None:
        self.app = app
        self.model_name = model_name

    async def on_llm_end(self, context: RunContextWrapper[RunContext], agent, response) -> None:
        usage = response.usage
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        reasoning = (
            getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        )
        self.app.runs.add_usage(
            context.context.run_id,
            requests=1,
            input_tokens=usage.input_tokens,
            cached_tokens=cached,
            output_tokens=usage.output_tokens,
            reasoning_tokens=reasoning,
            cost_usd=cost_usd(
                self.model_name,
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                cached_tokens=cached,
                cache_write_tokens=written,
            ),
        )

    async def on_tool_start(self, context: RunContextWrapper[RunContext], agent, tool) -> None:
        self.app.runs.add_tool_call(context.context.run_id)


class Harness:
    def __init__(self, app: App) -> None:
        self.app = app

    # ---- classification ----------------------------------------------------------------
    @staticmethod
    def profile_for(batch: Batch) -> str:
        if not batch.trusted:
            return "untrusted"
        background = all(e.kind in (Kind.TIMER, Kind.SYSTEM) for e in batch.events)
        if background:
            if any(
                e.kind == Kind.SYSTEM and e.payload.get("name") == "dream" for e in batch.events
            ):
                return "dream"
            return "scheduled"
        return "full"

    def origin_for(self, batch: Batch) -> Origin:
        for event in reversed(batch.events):
            if event.kind == Kind.MESSAGE and event.trust is Trust.OWNER:
                return Origin(
                    channel=event.source, recipient=event.payload.get("recipient"), interactive=True
                )
        return Origin(channel=DEFAULT_NOTIFY_CHANNEL, interactive=False)

    # ---- one run -----------------------------------------------------------------------
    async def run(self, batch: Batch) -> RunOutcome:
        """Run one batch of events. Returns an outcome for every ordinary ending (including a
        run that hit its step limit); raises for failures the inbox should schedule a retry for."""
        app = self.app
        profile = self.profile_for(batch)
        origin = self.origin_for(batch)
        model_name = app.config.model.main
        run_id = app.runs.start(
            session_key=batch.session_key,
            trigger=",".join(batch.kinds),
            profile=profile,
            model=model_name,
            event_ids=batch.ids,
            tainted=not batch.trusted,
        )
        ctx = RunContext(
            app=app,
            run_id=run_id,
            session_key=batch.session_key,
            profile=profile,
            origin=origin,
            trust=Trust.OWNER if batch.trusted else Trust.UNTRUSTED,
            tainted=not batch.trusted,
        )
        try:
            return await self._execute(batch, ctx, model_name)
        except asyncio.CancelledError:
            app.runs.finish(run_id, "cancelled", error="cancelled")
            raise
        except Exception as exc:
            failure = classify(exc)
            app.runs.finish(run_id, "failed", error=failure.summary)
            log.warning("run %s failed: %s", run_id, failure.summary)
            raise

    async def _execute(self, batch: Batch, ctx: RunContext, model_name: str) -> RunOutcome:
        app, cfg = self.app, self.app.config
        key, run_id = batch.session_key, ctx.run_id

        # The owner's rules are re-read at the start of every run. If they exist but cannot be
        # read, doing nothing is the only safe choice.
        try:
            prefs = app.memory.load_preferences()
        except PreferencesUnavailable as exc:
            raise PreferencesProblem(str(exc)) from exc
        instructions = build_instructions(
            agent=cfg.agent, profile=ctx.profile, prefs=prefs, index=app.memory.load_index()
        )
        # Build the model before touching any state: with no API key this raises, and a later
        # retry must find the session exactly as it was.
        model = app.model_factory(model_name)

        # Tool calls left without a result by a crash get an explicit "interrupted" result.
        # This must happen before new items are appended so each result follows its call.
        app.sessions.repair_dangling_calls(key, run_id=run_id)

        # Persist the triggering events first (idempotently), then run on the log alone. A retry
        # after a crash therefore resumes instead of duplicating the owner's message.
        context = context_block(
            now=app.clock.now(),
            tz=app.tz,
            trigger=describe_trigger(batch.events),
            session_key=key,
            notes=self._outbox_notes(),
        )
        fresh = [e for e in batch.events if not app.sessions.has_event(key, e.id)]
        if fresh:
            for position, event in enumerate(fresh):
                item = event_to_item(event, context=context if position == 0 else None)
                app.sessions.append(key, [item], event_id=event.id, run_id=run_id)
        else:
            app.sessions.append(
                key, [{"role": "user", "content": f"{context}\n\n{RESUME_NOTE}"}], run_id=run_id
            )

        agent = Agent(
            name=cfg.agent.name,
            instructions=instructions,
            model=model,
            model_settings=model_settings(cfg.model),
            tools=[t.tool for t in app.tools.for_profile(ctx.profile)],
        )
        try:
            result = await Runner.run(
                agent,
                [],
                session=SdkSession(app.sessions, key, run_id, window=cfg.agent.history_items),
                context=ctx,
                hooks=RunAccounting(app, model_name),
                max_turns=cfg.agent.max_turns,
                run_config=RunConfig(
                    tracing_disabled=True,
                    trace_include_sensitive_data=False,
                    tool_not_found_behavior="return_error_to_model",
                    reasoning_item_id_policy=cfg.model.reasoning_item_ids,
                ),
            )
        except MaxTurnsExceeded:
            return self._finish_step_limit(ctx)

        final_text = str(result.final_output or "").strip()
        return self._finish(ctx, final_text)

    # ---- endings -----------------------------------------------------------------------
    def _finish(self, ctx: RunContext, final_text: str) -> RunOutcome:
        app = self.app
        queued = list(ctx.queued_messages)
        if ctx.origin.interactive and ctx.profile == "full":
            reply = final_text or "(I finished without a reply. Ask again if you expected one.)"
            message_id = app.outbox.enqueue(
                session_key=ctx.session_key,
                channel=ctx.origin.channel,
                recipient=ctx.origin.recipient,
                text=reply,
                kind="message" if final_text else "notice",
                run_id=ctx.run_id,
                dedupe_key=f"run:{ctx.run_id}:final",
            )
            if message_id is not None:
                queued.append(message_id)
        # Background runs deliver nothing unless the agent explicitly called send_message.
        status = "completed" if queued else "silent"
        app.runs.finish(ctx.run_id, status, final_text=final_text or None)
        return RunOutcome(ctx.run_id, status, final_text, queued)

    def _finish_step_limit(self, ctx: RunContext) -> RunOutcome:
        app = self.app
        limit = app.config.agent.max_turns
        text = (
            f"I stopped after {limit} steps without finishing. Everything I did so far is saved; "
            "tell me to continue and I will pick up where I left off."
        )
        message_id = app.outbox.enqueue(
            session_key=ctx.session_key,
            channel=ctx.origin.channel,
            recipient=ctx.origin.recipient,
            text=text,
            kind="notice",
            run_id=ctx.run_id,
            dedupe_key=f"run:{ctx.run_id}:step-limit",
        )
        app.runs.finish(ctx.run_id, "failed", error=f"step limit ({limit}) reached")
        return RunOutcome(
            ctx.run_id, "failed", "", [message_id] if message_id else [], error="step limit reached"
        )

    def _outbox_notes(self) -> list[str]:
        unresolved = self.app.outbox.unresolved()
        if not unresolved:
            return []
        shown = "; ".join(f"#{m.id} [{m.status}] {m.text[:60]!r}" for m in unresolved[-5:])
        return [f"{len(unresolved)} earlier message(s) may not have reached the owner: {shown}"]
