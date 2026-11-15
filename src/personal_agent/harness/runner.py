"""The harness: wake up, rebuild context from durable state, run the agent loop, stop.

It keeps no state between runs. Everything it needs is read at the start of a run (preferences
fresh, memory index, session history), and everything it produces goes to durable stores
(session log, outbox, run record). A crash at any point leaves a log that the next run can
continue from.

A run that needs the owner's approval is *suspended*, not blocked: its state is saved, the
owner is asked through the outbox, and the process is free. An `approval_response` event later
restores the run and continues it with the decision.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agents import Agent, RunConfig, RunContextWrapper, RunHooks, Runner, RunResult, RunState
from agents.exceptions import MaxTurnsExceeded

from personal_agent.core.approvals import (
    DENIED,
    EXPIRED,
    SUPERSEDED_MESSAGE,
    PendingCall,
    request_text,
)
from personal_agent.core.budget import BudgetExceeded
from personal_agent.core.events import Batch, Event, Kind, Trust
from personal_agent.core.sessions import SdkSession
from personal_agent.harness.errors import PreferencesProblem, RunTimedOut, classify
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
from personal_agent.security.audit import safe_args
from personal_agent.tools.guard import ToolGuard, parse_args, summarize

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

RESUME_NOTE = (
    "[The previous attempt at this wake-up was interrupted. Continue from where the conversation "
    "stands; do not repeat actions that already completed.]"
)


@dataclass
class RunOutcome:
    run_id: str
    status: str  # completed | silent | failed | awaiting_approval
    final_text: str = ""
    queued_message_ids: list[int] = field(default_factory=list)
    error: str | None = None


class RunAccounting(RunHooks[RunContext]):
    """Writes usage and tool-call counts after every model call and tool call, so that cost is
    recorded even if the run later crashes, and enforces the spending limits.

    Limits are checked *before* each model call, never after: an answer that has been paid for
    is never thrown away, and spending can exceed a limit by at most one call."""

    def __init__(self, app: App, model_name: str) -> None:
        self.app = app
        self.model_name = model_name

    async def on_llm_start(
        self, context: RunContextWrapper[RunContext], agent, system_prompt, input_items
    ) -> None:
        self.app.budget.check_run(context.context.run_id)  # raises BudgetExceeded

    async def on_llm_end(self, context: RunContextWrapper[RunContext], agent, response) -> None:
        usage = response.usage
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        reasoning = (
            getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        )
        run_id = context.context.run_id
        self.app.runs.add_usage(
            run_id,
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


def _call_info(item: Any) -> tuple[str, str, str]:
    """(tool name, call id, raw JSON arguments) of an SDK approval item."""
    raw = item.raw_item
    get = raw.get if isinstance(raw, dict) else lambda k, d=None: getattr(raw, k, d)
    name = getattr(item, "tool_name", None) or get("name", "") or ""
    return (
        str(name),
        str(get("call_id", "") or get("id", "") or ""),
        str(get("arguments", "") or ""),
    )


class Harness:
    def __init__(self, app: App) -> None:
        self.app = app
        self.guard = ToolGuard(app)

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
        return Origin(channel=self.app.config.notify.channel, interactive=False)

    @staticmethod
    def owner_request_for(batch: Batch) -> str:
        """The newest thing the owner (or a timer they or the agent set) asked for, as trusted text."""
        for event in reversed(batch.events):
            if event.trust is not Trust.OWNER:
                continue
            if event.kind == Kind.MESSAGE and event.text:
                return event.text
            if event.kind == Kind.TIMER and isinstance(event.payload.get("prompt"), str):
                return event.payload["prompt"]
        return ""

    # ---- entry point -------------------------------------------------------------------
    async def run(self, batch: Batch) -> RunOutcome:
        """Handle one batch of events. Returns an outcome for every ordinary ending (including a
        run that hit its step limit or is waiting for approval); raises for failures that the
        inbox should schedule a retry for."""
        app = self.app
        app.budget.check_day()  # before any state changes: deferred events stay exactly as they were

        resumes = [e for e in batch.events if e.kind == Kind.APPROVAL_RESPONSE]
        rest = [e for e in batch.events if e.kind != Kind.APPROVAL_RESPONSE]
        outcomes: list[RunOutcome] = []
        for event in resumes:
            outcomes.append(await self._resume(event))
        if rest:
            sub = Batch(
                session_key=batch.session_key,
                owner=batch.owner,
                events=rest,
                lease_expires_at=batch.lease_expires_at,
            )
            outcomes.append(await self._run_events(sub))
        if not outcomes:
            return RunOutcome("-", "silent")
        last = outcomes[-1]
        queued = [m for o in outcomes for m in o.queued_message_ids]
        return RunOutcome(last.run_id, last.status, last.final_text, queued, last.error)

    # ---- a run triggered by new events ---------------------------------------------------
    async def _run_events(self, batch: Batch) -> RunOutcome:
        app = self.app
        profile = self.profile_for(batch)
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
            origin=self.origin_for(batch),
            trust=Trust.OWNER if batch.trusted else Trust.UNTRUSTED,
            tainted=not batch.trusted,
            owner_request=self.owner_request_for(batch),
        )
        return await self._settle(ctx, self._execute(batch, ctx, model_name))

    async def _settle(self, ctx: RunContext, work, *, resuming: bool = False) -> RunOutcome:
        """Await `work` and make sure the run record ends up truthful whatever happens."""
        app = self.app
        try:
            return await work
        except asyncio.CancelledError:
            if resuming:
                app.runs.set_status(ctx.run_id, "awaiting_approval")  # still resumable
            else:
                app.runs.finish(ctx.run_id, "cancelled", error="cancelled")
            raise
        except Exception as exc:
            failure = classify(exc)
            if resuming:
                app.runs.set_status(ctx.run_id, "awaiting_approval")
            elif isinstance(exc, BudgetExceeded) and exc.scope == "daily":
                app.runs.finish(ctx.run_id, "paused_budget", error=failure.summary)
            else:
                app.runs.finish(ctx.run_id, "failed", error=failure.summary)
            log.warning("run %s failed: %s", ctx.run_id, failure.summary)
            raise

    def _instructions(self, profile: str) -> str:
        app, cfg = self.app, self.app.config
        # The owner's rules are re-read at the start of every run. If they exist but cannot be
        # read, doing nothing is the only safe choice.
        try:
            prefs = app.memory.load_preferences()
        except PreferencesUnavailable as exc:
            raise PreferencesProblem(str(exc)) from exc
        return build_instructions(
            agent=cfg.agent, profile=profile, prefs=prefs, index=app.memory.load_index()
        )

    def _agent(self, ctx: RunContext, instructions: str, model) -> Agent:
        app, cfg = self.app, self.app.config
        return Agent(
            name=cfg.agent.name,
            instructions=instructions,
            model=model,
            model_settings=model_settings(cfg.model),
            tools=[
                self.guard.wrap(t)
                for t in app.tools.for_profile(ctx.profile)
                if self.guard.offered(t, ctx.profile)
            ],
        )

    async def _execute(self, batch: Batch, ctx: RunContext, model_name: str) -> RunOutcome:
        app = self.app
        key, run_id = batch.session_key, ctx.run_id

        instructions = self._instructions(ctx.profile)
        # Build the model before touching any state: with no API key this raises, and a later
        # retry must find the session exactly as it was.
        model = app.model_factory(model_name)

        # A new message from the owner means they moved on: requests still waiting for an answer
        # are closed (never executed) so the log has no unanswered tool calls.
        if any(e.kind == Kind.MESSAGE and e.trust is Trust.OWNER for e in batch.events):
            self._supersede(key, run_id)

        # Tool calls left without a result by a crash get an explicit "interrupted" result.
        # This must happen before new items are appended so each result follows its call.
        # Calls that belong to a run waiting for approval are not crashes; leave them alone.
        app.sessions.repair_dangling_calls(
            key, run_id=run_id, skip=app.approvals.suspended_call_ids(key)
        )

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

        agent = self._agent(ctx, instructions, model)
        return await self._drive(agent, [], ctx, model_name)

    # ---- driving the SDK loop ----------------------------------------------------------------
    async def _drive(
        self, agent: Agent, run_input: Any, ctx: RunContext, model_name: str
    ) -> RunOutcome:
        app, cfg = self.app, self.app.config
        resuming = isinstance(run_input, RunState)
        try:
            result: RunResult = await asyncio.wait_for(
                Runner.run(
                    agent,
                    run_input,
                    session=SdkSession(
                        app.sessions, ctx.session_key, ctx.run_id, window=cfg.agent.history_items
                    ),
                    context=None if resuming else ctx,
                    hooks=RunAccounting(app, model_name),
                    max_turns=cfg.agent.max_turns,
                    run_config=RunConfig(
                        tracing_disabled=True,
                        trace_include_sensitive_data=False,
                        tool_not_found_behavior="return_error_to_model",
                        reasoning_item_id_policy=cfg.model.reasoning_item_ids,
                    ),
                ),
                timeout=cfg.runtime.run_timeout_s,
            )
        except MaxTurnsExceeded:
            return self._finish_step_limit(ctx)
        except BudgetExceeded as exc:
            if exc.scope == "run":
                return self._finish_run_budget(ctx, exc)
            raise
        except TimeoutError as exc:
            raise RunTimedOut(f"the run exceeded {cfg.runtime.run_timeout_s:.0f}s") from exc

        if result.interruptions:
            return self._suspend(ctx, result)
        app.approvals.drop_run(ctx.run_id)
        return self._finish(ctx, str(result.final_output or "").strip())

    # ---- waiting for the owner ----------------------------------------------------------------
    def _suspend(self, ctx: RunContext, result: RunResult) -> RunOutcome:
        """Save the run and ask the owner. Everything happens in one transaction: either the run
        is suspended *and* the owner has been queued a request, or neither."""
        app = self.app
        state = result.to_state().to_string(context_serializer=lambda c: {"run_id": c.run_id})
        calls: list[PendingCall] = []
        for item in result.interruptions:
            name, call_id, raw = _call_info(item)
            tool = app.tools.get(name)
            args = parse_args(raw)
            calls.append(
                PendingCall(call_id, name, summarize(tool, args) if tool else safe_args(args)[:300])
            )
        with app.db.tx():
            approvals = app.approvals.create(ctx.run_id, ctx.session_key, calls)
            app.approvals.save_run(ctx.run_id, ctx.session_key, state, ctx.to_meta())
            message_id = app.outbox.enqueue(
                session_key=ctx.session_key,
                channel=ctx.origin.channel,
                recipient=ctx.origin.recipient,
                text=request_text(approvals),
                kind="approval_request",
                run_id=ctx.run_id,
                dedupe_key=f"approval:{ctx.run_id}:{approvals[0].id}",
                extra={
                    "approval_ids": [a.id for a in approvals],
                    "interactive": ctx.origin.interactive,
                },
            )
            app.runs.set_status(ctx.run_id, "awaiting_approval")
        queued = list(ctx.queued_messages) + ([message_id] if message_id else [])
        return RunOutcome(ctx.run_id, "awaiting_approval", "", queued)

    async def _resume(self, event: Event) -> RunOutcome:
        app = self.app
        run_id = str(event.payload.get("run_id") or "")
        saved = app.approvals.load_run(run_id)
        if saved is None:
            # Already resumed, or the owner moved on and the request was closed. Nothing to do.
            return RunOutcome(run_id or "-", "silent", error="nothing to resume")
        run = app.runs.get(run_id)
        model_name = (run.model if run else None) or app.config.model.main
        ctx = RunContext.from_meta(app, run_id, saved.session_key, saved.meta)
        app.runs.set_status(run_id, "running")
        return await self._settle(ctx, self._continue(ctx, saved.state, model_name), resuming=True)

    async def _continue(self, ctx: RunContext, state_text: str, model_name: str) -> RunOutcome:
        app = self.app
        instructions = self._instructions(ctx.profile)
        model = app.model_factory(model_name)
        agent = self._agent(ctx, instructions, model)
        state = await RunState.from_string(agent, state_text, context_override=ctx)

        by_call = {a.call_id: a for a in app.approvals.for_run(ctx.run_id)}
        for item in state.get_interruptions():
            _, call_id, _ = _call_info(item)
            approval = by_call.get(call_id)
            if approval is not None and approval.status == "approved":
                state.approve(item)
                continue
            status = approval.status if approval else DENIED
            reason = approval.rejection_message() if approval else None
            state.reject(item, rejection_message=reason)
            app.audit.set_outcome(
                ctx.run_id, call_id, "expired" if status == EXPIRED else "rejected"
            )
        return await self._drive(agent, state, ctx, model_name)

    def _supersede(self, key: str, run_id: str) -> None:
        """The owner sent a new message while a request was waiting: close it, never run it."""
        app = self.app
        closed = app.approvals.supersede_session(key)
        for run in {a.run_id for a in closed}:
            for approval in app.approvals.for_run(run):
                app.sessions.append(
                    key,
                    [
                        {
                            "type": "function_call_output",
                            "call_id": approval.call_id,
                            "output": SUPERSEDED_MESSAGE,
                        }
                    ],
                    run_id=run_id,
                )
                app.audit.set_outcome(run, approval.call_id, "superseded")
            app.approvals.drop_run(run)
            app.runs.finish(run, "cancelled", error="superseded by a new message")

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
                extra={"interactive": True},
            )
            if message_id is not None:
                queued.append(message_id)
        # Background runs deliver nothing unless the agent explicitly called send_message.
        status = "completed" if queued else "silent"
        app.runs.finish(ctx.run_id, status, final_text=final_text or None)
        return RunOutcome(ctx.run_id, status, final_text, queued)

    def _stop(self, ctx: RunContext, *, text: str, key: str, error: str) -> RunOutcome:
        """End a run that was stopped on purpose: tell the owner, record why."""
        app = self.app
        app.approvals.drop_run(ctx.run_id)
        message_id = app.outbox.enqueue(
            session_key=ctx.session_key,
            channel=ctx.origin.channel,
            recipient=ctx.origin.recipient,
            text=text,
            kind="notice",
            run_id=ctx.run_id,
            dedupe_key=f"run:{ctx.run_id}:{key}",
            extra={"interactive": ctx.origin.interactive},
        )
        app.runs.finish(ctx.run_id, "failed", error=error)
        return RunOutcome(
            ctx.run_id,
            "failed",
            "",
            [*ctx.queued_messages, *([message_id] if message_id else [])],
            error,
        )

    def _finish_step_limit(self, ctx: RunContext) -> RunOutcome:
        limit = self.app.config.agent.max_turns
        return self._stop(
            ctx,
            text=(
                f"I stopped after {limit} steps without finishing. Everything I did so far is saved; "
                "tell me to continue and I will pick up where I left off."
            ),
            key="step-limit",
            error=f"step limit ({limit}) reached",
        )

    def _finish_run_budget(self, ctx: RunContext, exc: BudgetExceeded) -> RunOutcome:
        return self._stop(
            ctx,
            text=(
                f"I stopped this task because it reached its cost limit (${exc.limit:.2f}; "
                f"it used ${exc.spent:.2f}). Everything I did so far is saved. Tell me to continue "
                "if you want me to keep going."
            ),
            key="run-budget",
            error=f"run budget reached (${exc.spent:.2f} of ${exc.limit:.2f})",
        )

    def _outbox_notes(self) -> list[str]:
        unresolved = self.app.outbox.unresolved()
        if not unresolved:
            return []
        shown = "; ".join(f"#{m.id} [{m.status}] {m.text[:60]!r}" for m in unresolved[-5:])
        return [f"{len(unresolved)} earlier message(s) may not have reached the owner: {shown}"]
