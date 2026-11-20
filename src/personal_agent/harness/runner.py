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
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from agents import (
    Agent,
    ModelResponse,
    RunConfig,
    RunContextWrapper,
    RunHooks,
    Runner,
    RunResult,
    RunResultStreaming,
    RunState,
    Tool,
    TResponseInputItem,
)
from agents.exceptions import MaxTurnsExceeded
from agents.models.interface import Model

from personal_agent.collaboration.models import AccessDenied
from personal_agent.core.approvals import (
    DENIED,
    EXPIRED,
    SUPERSEDED_MESSAGE,
    UNAVAILABLE_MESSAGE,
    PendingCall,
    request_text,
)
from personal_agent.core.budget import BudgetExceeded
from personal_agent.core.events import Batch, Event, Kind, Trust
from personal_agent.core.sessions import SdkSession
from personal_agent.harness.compaction import Compactor
from personal_agent.harness.dreaming import Dreamer
from personal_agent.harness.errors import PreferencesProblem, RunTimedOut, classify
from personal_agent.harness.instructions import (
    build_instructions,
    context_block,
    describe_trigger,
    event_to_item,
    recent_messages_block,
)
from personal_agent.harness.models import cache_key_for, model_settings, shares_prompt_cache
from personal_agent.harness.pricing import cost_usd
from personal_agent.harness.runctx import Origin, RunContext
from personal_agent.harness.streaming import consume_stream
from personal_agent.memory.store import PreferencesUnavailable
from personal_agent.tools.guard import ToolGuard, parse_args, summarize
from personal_agent.tools.skills import shell_dir
from personal_agent.tools.urls import find_urls
from personal_agent.tools.web import hosted_tools, record_hosted_search

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

MAX_REQUESTED_SKILLS = 3
"""Skills one message can pull in by name; more would drown the request."""

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
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        system_prompt: str | None,
        input_items: list[TResponseInputItem],
    ) -> None:
        ctx = context.context
        ctx.require_authorized()
        self.app.budget.check_run(ctx.run_id, ctx.cost_cap_usd)  # raises BudgetExceeded

    async def on_llm_end(
        self,
        context: RunContextWrapper[RunContext],
        agent: Agent[RunContext],
        response: ModelResponse,
    ) -> None:
        usage = response.usage
        details = getattr(usage, "input_tokens_details", None)
        cached = getattr(details, "cached_tokens", 0) or 0
        written = getattr(details, "cache_write_tokens", 0) or 0
        reasoning = (
            getattr(getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0) or 0
        )
        run_id = context.context.run_id
        record_hosted_search(self.app, context.context, response)
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

    async def on_tool_start(
        self, context: RunContextWrapper[RunContext], agent: Agent[RunContext], tool: Tool
    ) -> None:
        self.app.runs.add_tool_call(context.context.run_id)
        self.app.live.tool_started(str(getattr(tool, "name", "")))


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

    OWN_SESSION_PREFIXES = ("timer:", "webhook:", "system:")
    """Session keys that belong to one source of wake-ups (`timer:NAME`, `webhook:ROUTE`,
    `system:NAME`), as opposed to a conversation's own key such as `main`."""

    @classmethod
    def starts_new_session(cls, batch: Batch) -> bool:
        """Whether this wake-up begins a conversation of its own.

        Events that continue earlier work resume the session they belong to: the owner writing, an
        answer to a request for approval, a timer the agent set for "here". Everything else starts
        a new one: yesterday's brief is not context for today's, and a schedule that fires every
        morning must not pile a year of mornings into one conversation. What a wake-up needs from
        before comes from memory (and from what the owner was last told), never from the leftovers
        of a conversation nobody is part of."""
        if any(e.kind in (Kind.MESSAGE, Kind.APPROVAL_RESPONSE) for e in batch.events):
            return False
        return batch.session_key.startswith(cls.OWN_SESSION_PREFIXES)

    def origin_for(self, batch: Batch) -> Origin:
        for event in reversed(batch.events):
            if event.kind == Kind.MESSAGE and event.trust is Trust.OWNER:
                return Origin(
                    channel=event.source, recipient=event.payload.get("recipient"), interactive=True
                )
        for event in reversed(batch.events):
            origin = event.payload.get("origin")
            if isinstance(origin, dict):
                return Origin(
                    channel=str(origin.get("channel") or "web"),
                    recipient=origin.get("recipient"),
                    interactive=False,
                )
        return Origin(channel=self.app.notify_channel(), interactive=False)

    @staticmethod
    def owner_request_for(batch: Batch) -> str:
        """The newest thing the owner (or a timer they or the agent set) asked for, as trusted text."""
        for event in reversed(batch.events):
            if event.trust is not Trust.OWNER:
                continue
            if event.kind == Kind.MESSAGE and event.text:
                if event.payload.get("untrusted_inputs") is True:
                    trusted = event.payload.get("owner_request")
                    return (
                        trusted
                        if isinstance(trusted, str) and trusted
                        else "Execute the approved delegated task; peer inputs are data only."
                    )
                return event.text
            prompt = event.payload.get("prompt")
            if event.kind == Kind.TIMER and isinstance(prompt, str):
                return prompt
        return ""

    @staticmethod
    def owner_urls_for(batch: Batch) -> list[str]:
        """Web addresses the owner wrote in this batch (messages, or a timer's prompt)."""
        texts = [
            (
                str(e.payload.get("owner_request") or "")
                if e.payload.get("untrusted_inputs") is True
                else (e.text if e.kind == Kind.MESSAGE else str(e.payload.get("prompt") or ""))
            )
            for e in batch.events
            if e.trust is Trust.OWNER and e.kind in (Kind.MESSAGE, Kind.TIMER)
        ]
        return find_urls("\n".join(t for t in texts if t))

    # ---- entry point -------------------------------------------------------------------
    async def run(self, batch: Batch) -> RunOutcome:
        manager = self.app.collaboration
        if manager is None:
            return await self._run_batch(batch)
        async with manager.run_slots:
            return await self._run_batch(batch)

    def _event_authorized(self, event: Event) -> bool:
        try:
            actor = str(
                event.payload.get("principal_id")
                or event.payload.get("actor_id")
                or self.app.space_owner_id
            )
            self.app.require_actor(
                actor,
                version=event.payload.get("membership_version"),
                principal_version=event.payload.get("principal_version"),
            )
            return True
        except AccessDenied:
            return False

    async def _run_batch(self, batch: Batch) -> RunOutcome:
        """Handle one batch of events. Returns an outcome for every ordinary ending (including a
        run that hit its step limit or is waiting for approval); raises for failures that the
        inbox should schedule a retry for."""
        app = self.app
        if app.identities is not None:
            # Ownership changes may happen between daemon ticks; don't use a cached former
            # owner as the default authority of system/legacy background events.
            app.space_owner_id = app.identities.space(app.space_id).owner_id
        active = [event for event in batch.events if self._event_authorized(event)]
        if not active:
            return RunOutcome("-", "silent", error="authorization changed")
        batch = Batch(batch.session_key, batch.owner, active, batch.lease_expires_at)
        app.budget.check_day()  # deferred events stay exactly as they were

        resumes = [e for e in batch.events if e.kind == Kind.APPROVAL_RESPONSE]
        done = app.runs.handled(
            batch.session_key, batch.ids, since=min(e.created_at for e in batch.events)
        )
        if done:
            log.info("events %s were already handled by an earlier attempt", sorted(done))
        rest = [e for e in batch.events if e.kind != Kind.APPROVAL_RESPONSE and e.id not in done]
        outcomes: list[RunOutcome] = []
        for event in resumes:
            outcomes.append(await self._resume(event))
        # Shared conversations may contain several people. Never grant the newest person's
        # authority or return address to somebody else's event in the same inbox claim.
        from itertools import groupby

        def actor_key(event: Event) -> tuple[Any, ...]:
            if "principal_id" not in event.payload and "actor_id" not in event.payload:
                return (app.space_owner_id, "legacy")
            return (
                event.payload.get("principal_id")
                or event.payload.get("actor_id")
                or app.space_owner_id,
                event.payload.get("membership_version"),
                event.payload.get("principal_version"),
                event.source,
                event.payload.get("recipient"),
                str(event.payload.get("origin", "")),
            )

        for _, events in groupby(rest, key=actor_key):
            sub = Batch(batch.session_key, batch.owner, list(events), batch.lease_expires_at)
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
        initial_taint = not batch.trusted or any(
            event.payload.get("untrusted_inputs") is True for event in batch.events
        )
        run_id = app.runs.start(
            session_key=batch.session_key,
            trigger=",".join(batch.kinds),
            profile=profile,
            model=model_name,
            event_ids=batch.ids,
            tainted=initial_taint,
        )
        ctx = RunContext(
            app=app,
            run_id=run_id,
            session_key=batch.session_key,
            profile=profile,
            origin=self.origin_for(batch),
            trust=Trust.OWNER if batch.trusted else Trust.UNTRUSTED,
            tainted=initial_taint,
            work_id=f"events:{min(batch.ids)}",
            owner_request=self.owner_request_for(batch),
            principal_id=str(
                batch.events[-1].payload.get("principal_id")
                or batch.events[-1].payload.get("actor_id")
                or app.space_owner_id
            ),
            membership_version=batch.events[-1].payload.get("membership_version"),
            principal_version=batch.events[-1].payload.get("principal_version"),
        )
        ctx.know_urls(self.owner_urls_for(batch))
        dreamer = Dreamer(app) if profile == "dream" else None
        if profile == "dream":
            ctx.cost_cap_usd = app.config.dreaming.max_cost_usd
        if dreamer is not None:
            dreamer.begin(run_id, batch)
        try:
            outcome = await self._settle(ctx, self._execute(batch, ctx, model_name))
        except BaseException:
            if dreamer is not None:
                dreamer.abort(run_id)
            raise
        if dreamer is not None:
            dreamer.finished(batch, outcome)
        return outcome

    async def _with_deadline(self, work: Awaitable[RunOutcome]) -> RunOutcome:
        """One deadline includes plugin preparation and compaction, not only model turns."""
        seconds = self.app.config.runtime.run_timeout_s
        deadline = asyncio.timeout(seconds)
        try:
            async with deadline:
                return await work
        except TimeoutError as exc:
            if deadline.expired():
                raise RunTimedOut(f"the run exceeded {seconds:.0f}s") from exc
            raise

    async def _settle(
        self, ctx: RunContext, work: Awaitable[RunOutcome], *, resuming: bool = False
    ) -> RunOutcome:
        """Await `work` and make sure the run record ends up truthful whatever happens."""
        app = self.app
        try:
            return await self._with_deadline(work)
        except asyncio.CancelledError:
            if resuming and ctx.profile != "dream":
                app.runs.set_status(ctx.run_id, "awaiting_approval")  # still resumable
            else:
                with app.db.tx():
                    if resuming:
                        app.approvals.drop_run(ctx.run_id)
                    app.runs.finish(ctx.run_id, "cancelled", error="cancelled")
            raise
        except AccessDenied:
            with app.db.tx():
                app.approvals.drop_run(ctx.run_id)
                app.runs.finish(ctx.run_id, "cancelled", error="authorization changed")
            return RunOutcome(ctx.run_id, "cancelled", error="authorization changed")
        except Exception as exc:
            failure = classify(exc)
            if resuming and ctx.profile != "dream":
                app.runs.set_status(ctx.run_id, "awaiting_approval")
            else:
                with app.db.tx():
                    if resuming:
                        app.approvals.drop_run(ctx.run_id)
                    status = (
                        "paused_budget"
                        if isinstance(exc, BudgetExceeded) and exc.scope == "daily"
                        else "failed"
                    )
                    app.runs.finish(ctx.run_id, status, error=failure.summary)
            log.warning("run %s failed: %s", ctx.run_id, failure.summary)
            raise
        finally:
            if resuming and ctx.profile == "dream":
                Dreamer(app).finish_run(ctx.run_id, notify_interrupted=True)

    def _instructions(self, profile: str) -> str:
        app, cfg = self.app, self.app.config
        # The owner's rules are re-read at the start of every run. If they exist but cannot be
        # read, doing nothing is the only safe choice.
        try:
            prefs = app.memory.load_preferences()
        except PreferencesUnavailable as exc:
            raise PreferencesProblem(str(exc)) from exc
        return build_instructions(
            agent=cfg.agent,
            profile=profile,
            prefs=prefs,
            index=app.memory.load_index(),
            skills=self._skills_block(profile),
            tasks=app.memory.load_task_index(),
        )

    async def _prepare_plugins(self) -> None:
        """Bring the MCP tools up to date. A server that is down costs its own tools, not the run."""
        try:
            await self.app.mcp.prepare()
        except Exception:
            log.warning("could not prepare the MCP servers", exc_info=True)

    def _skills_block(self, profile: str) -> str:
        """The list of installed skills, only for runs that can load one."""
        tool = self.app.tools.get("skill")
        if tool is None or not self.guard.offered(tool, profile):
            return ""
        return self.app.skills.catalog()

    def _requested_skills(self, event: Event) -> str:
        """Skills the owner asked for by name (`$name`) in a message: their instructions are put
        into the same session item, as if the model had loaded them itself."""
        if event.kind != Kind.MESSAGE or event.trust is not Trust.OWNER:
            return ""
        app = self.app
        blocks: list[str] = []
        for skill in app.skills.mentioned(event.text)[:MAX_REQUESTED_SKILLS]:
            try:
                text = app.skills.instructions(skill, shell_dir=shell_dir(app, skill))
            except Exception:
                log.warning("could not load skill %s", skill.name, exc_info=True)
                continue
            blocks.append(
                f"[The owner asked you to use the skill ${skill.name}. Its instructions:]\n{text}"
            )
        return "\n\n".join(blocks)

    def _agent(self, ctx: RunContext, instructions: str, model: Model) -> Agent[RunContext]:
        app, cfg = self.app, self.app.config
        hosted = hosted_tools(app, ctx.profile)
        cache_key = cache_key_for(ctx.profile) if shares_prompt_cache(model) else None
        return Agent(
            name=cfg.agent.name,
            instructions=instructions,
            model=model,
            model_settings=model_settings(
                cfg.model, hosted_search=bool(hosted), cache_key=cache_key
            ),
            tools=[
                *(
                    self.guard.wrap(t)
                    for t in app.tools.for_profile(ctx.profile)
                    if self.guard.offered(t, ctx.profile)
                ),
                *hosted,
            ],
        )

    async def _execute(self, batch: Batch, ctx: RunContext, model_name: str) -> RunOutcome:
        app = self.app
        key, run_id = batch.session_key, ctx.run_id

        await self._prepare_plugins()
        ctx.require_authorized()
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

        # A wake-up that stands on its own starts a new conversation. Only on the first attempt:
        # a retry after a crash finds some of its events already in the log and must resume that
        # work, not hide it. A conversation waiting for the owner's approval is left alone too.
        fresh = [e for e in batch.events if not app.sessions.has_event(key, e.id)]
        if (
            len(fresh) == len(batch.events)
            and self.starts_new_session(batch)
            and not app.approvals.suspended_call_ids(key)
            and not app.sessions.is_blank(key)
        ):
            app.sessions.clear(key)
        elif len(fresh) == len(batch.events):
            # A conversation that carries on and has grown long is summarised before the new
            # words are added, so that the summary never covers them.
            await Compactor(app).compact(key, run_id, authorize=ctx.require_authorized)
            ctx.require_authorized()

        # Persist the triggering events first (idempotently), then run on the log alone. A retry
        # after a crash therefore resumes instead of duplicating the owner's message.
        context = context_block(
            now=app.clock.now(),
            tz=app.tz,
            trigger=describe_trigger(batch.events),
            session_key=key,
            notes=[*self._outbox_notes(), *app.mcp.notes()],
            recent=self._recently_sent(batch),
        )
        if fresh:
            for position, event in enumerate(fresh):
                item = event_to_item(
                    event,
                    context=context if position == 0 else None,
                    extra=self._requested_skills(event),
                )
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
            stream = (
                cfg.web.enabled
                and cfg.web.stream_responses
                and ctx.origin.channel == "web"
                and ctx.origin.interactive
                and ctx.profile == "full"
            )
            options: dict[str, Any] = dict(
                session=SdkSession(
                    app.sessions,
                    ctx.session_key,
                    ctx.run_id,
                    window=cfg.agent.history_items,
                    media=app.media,
                    keep_images=cfg.media.images_in_context,
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
            )
            result: RunResult | RunResultStreaming
            if stream:
                result = Runner.run_streamed(agent, run_input, **options)
                await consume_stream(result, app.live, ctx.run_id)
            else:
                result = await Runner.run(agent, run_input, **options)
        except MaxTurnsExceeded:
            return self._finish_step_limit(ctx)
        except BudgetExceeded as exc:
            if exc.scope == "run":
                return self._finish_run_budget(ctx, exc)
            raise

        if result.interruptions:
            return self._suspend(ctx, result)
        return self._finish(ctx, str(result.final_output or "").strip())

    # ---- waiting for the owner ----------------------------------------------------------------
    def _suspend(self, ctx: RunContext, result: RunResult | RunResultStreaming) -> RunOutcome:
        """Save the run and ask the owner. Everything happens in one transaction: either the run
        is suspended *and* the owner has been queued a request, or neither."""
        app = self.app
        ctx.require_authorized()
        state = result.to_state().to_string(context_serializer=lambda c: {"run_id": c.run_id})
        calls: list[PendingCall] = []
        for item in result.interruptions:
            name, call_id, raw = _call_info(item)
            tool = app.tools.get(name)
            args = parse_args(raw)
            calls.append(PendingCall(call_id, name, summarize(tool, args)))
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
                    **ctx.authority(),
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
        if not saved.accepts(event):
            return RunOutcome(run_id, "silent", error="stale approval response")
        if any(a.status == "pending" for a in app.approvals.for_run(run_id)):
            # A retry may find the next approval round already saved. Pending is never a refusal.
            return RunOutcome(run_id, "awaiting_approval", error="approval still pending")
        run = app.runs.get(run_id)
        model_name = (run.model if run else None) or app.config.model.main
        ctx = RunContext.from_meta(app, run_id, saved.session_key, saved.meta)
        try:
            ctx.require_authorized()
        except AccessDenied:
            app.approvals.drop_run(run_id)
            app.runs.finish(run_id, "cancelled", error="authorization changed")
            return RunOutcome(run_id, "cancelled", error="authorization changed")
        app.runs.set_status(run_id, "running")
        return await self._settle(ctx, self._continue(ctx, saved.state, model_name), resuming=True)

    async def _continue(self, ctx: RunContext, state_text: str, model_name: str) -> RunOutcome:
        app = self.app
        await self._prepare_plugins()
        ctx.require_authorized()
        instructions = self._instructions(ctx.profile)
        model = app.model_factory(model_name)
        agent = self._agent(ctx, instructions, model)
        state = await RunState.from_string(
            agent, state_text, context_override=RunContextWrapper(context=ctx)
        )

        by_call = {a.call_id: a for a in app.approvals.for_run(ctx.run_id)}
        offered = {getattr(t, "name", None) for t in agent.tools}
        for item in state.get_interruptions():
            name, call_id, _ = _call_info(item)
            approval = by_call.get(call_id)
            if approval is not None and approval.status == "approved":
                if name not in offered:
                    # Approved, but the tool is gone (its server is down, a plugin was removed).
                    # Say so, instead of letting the SDK report a missing tool.
                    state.reject(item, rejection_message=UNAVAILABLE_MESSAGE)
                    app.audit.set_outcome(ctx.run_id, call_id, "unavailable")
                    continue
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
        with app.db.tx():
            closed = app.approvals.supersede_session(key)
            answered = {
                item.get("call_id")
                for item in app.sessions.items(key)
                if item.get("type") == "function_call_output"
            }
            for run in {a.run_id for a in closed}:
                for approval in app.approvals.for_run(run):
                    if approval.call_id in answered:
                        continue  # an earlier round already ran; do not duplicate its result
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
                record = app.runs.get(run)
                if record is not None and record.profile == "dream":
                    Dreamer(app).finish_run(run, notify_interrupted=True)

    # ---- endings -----------------------------------------------------------------------
    def _finish(self, ctx: RunContext, final_text: str) -> RunOutcome:
        """Queue the answer and close the run, all in one commit. Split in two, a crash between the
        steps would either send the answer again after the retry, or (once the saved state of a run
        that waited for approval is gone) lose it."""
        app = self.app
        ctx.require_authorized()
        queued = list(ctx.queued_messages)
        with app.db.tx():
            app.approvals.drop_run(ctx.run_id)
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
                    extra={"interactive": True, "final_response": True, **ctx.authority()},
                )
                if message_id is not None:
                    queued.append(message_id)
            # Background runs deliver nothing unless the agent explicitly called send_message.
            status = "completed" if queued else "silent"
            app.runs.finish(ctx.run_id, status, final_text=final_text or None)
        return RunOutcome(ctx.run_id, status, final_text, queued)

    def _stop(self, ctx: RunContext, *, text: str, key: str, error: str) -> RunOutcome:
        """End a run that was stopped on purpose: tell the owner, record why. The nightly memory
        pass is the exception: it is not the owner's task, `Dreamer` counts its failures and says
        so once if they keep coming."""
        app = self.app
        with app.db.tx():
            app.approvals.drop_run(ctx.run_id)
            message_id = (
                None
                if ctx.profile == "dream"
                else app.outbox.enqueue(
                    session_key=ctx.session_key,
                    channel=ctx.origin.channel,
                    recipient=ctx.origin.recipient,
                    text=text,
                    kind="notice",
                    run_id=ctx.run_id,
                    dedupe_key=f"run:{ctx.run_id}:{key}",
                    extra={"interactive": ctx.origin.interactive, **ctx.authority()},
                )
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

    def _recently_sent(self, batch: Batch) -> list[str]:
        """What the owner was last told, for a background run that has no conversation behind it.

        A conversation with the owner already has its own history, and they would not expect it to
        be recited. A background run that starts from nothing could otherwise report the same news
        every time it wakes up."""
        cfg = self.app.config.memory
        if any(e.kind == Kind.MESSAGE for e in batch.events):
            return []
        if not self.app.sessions.is_blank(batch.session_key):
            return []
        return recent_messages_block(
            self.app.transcript.recent_messages(cfg.recent_messages),
            self.app.tz,
            cfg.recent_message_chars,
        )

    def _outbox_notes(self) -> list[str]:
        unresolved = self.app.outbox.unresolved()
        if not unresolved:
            return []
        shown = "; ".join(f"#{m.id} [{m.status}] {m.text[:60]!r}" for m in unresolved[-5:])
        return [f"{len(unresolved)} earlier message(s) may not have reached the owner: {shown}"]
