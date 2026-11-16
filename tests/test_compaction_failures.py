"""Per-response billing survives retry failures; both compaction and the whole run have deadlines."""

from __future__ import annotations

import asyncio

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message

from deadlines import Deadlines
from helpers import owner_message, usage
from personal_agent.config import BudgetConfig, RuntimeConfig
from personal_agent.core.worker import Worker
from personal_agent.harness import compaction
from personal_agent.harness.compaction import Compactor, LLMSummarizer
from personal_agent.harness.pricing import cost_usd
from test_compaction import config, seed


def prepare(make_app, summary_model, *, timeout=1, budget=1):
    cfg = config(timeout_s=timeout).model_copy(update={"budget": BudgetConfig(per_run_usd=budget)})
    app = make_app(config=cfg)
    app.summarizer = LLMSummarizer(lambda _: summary_model, cfg.model, cfg.context)
    seed(app)
    run_id = app.runs.start(
        session_key="main", trigger="message", profile="full", model=cfg.model.main, event_ids=[1]
    )
    return app, run_id


async def test_first_summary_response_stays_billed_when_retry_raises(make_app):
    model = ScriptedModel(
        [
            ModelStep(
                output=[assistant_message("")], usage=usage(100_000, 30, cached=5_000, reasoning=20)
            ),
            RuntimeError("second response failed"),
        ]
    )
    app, run_id = prepare(make_app, model)
    assert await Compactor(app).compact("main", run_id) is None
    run = app.runs.get(run_id)
    assert run.requests == 1 and run.input_tokens == 100_000
    assert run.cached_tokens == 5_000 and run.reasoning_tokens == 20
    assert run.cost_usd == pytest.approx(
        cost_usd(
            app.config.model.fast,
            input_tokens=100_000,
            output_tokens=30,
            cached_tokens=5_000,
        )
    )
    assert app.sessions.summary("main") is None and len(model.calls) == 2


@pytest.mark.parametrize("stop", ["timeout", "cancel"])
async def test_retry_interruption_does_not_erase_first_response_usage(make_app, monkeypatch, stop):
    waiting = asyncio.Event()
    deadlines = Deadlines(monkeypatch, compaction) if stop == "timeout" else None

    async def hang(call):
        waiting.set()
        await asyncio.Event().wait()

    model = ScriptedModel(
        [
            ModelStep(output=[assistant_message("")], usage=usage(10_000, 50)),
            ModelStep(responder=hang),
        ]
    )
    app, run_id = prepare(make_app, model, timeout=0.05 if stop == "timeout" else 5)
    compact = asyncio.create_task(Compactor(app).compact("main", run_id))
    await asyncio.wait_for(waiting.wait(), 2)
    if deadlines is not None:
        assert deadlines.seconds == [app.config.context.timeout_s]
        deadlines.expire()
    if stop == "cancel":
        compact.cancel()
        with pytest.raises(asyncio.CancelledError):
            await compact
    else:
        assert await compact is None
    run = app.runs.get(run_id)
    assert run.requests == 1 and run.cost_usd > 0
    assert app.sessions.summary("main") is None


async def test_summary_retry_checks_the_budget_after_its_first_charge(make_app):
    model = ScriptedModel(
        [
            ModelStep(output=[assistant_message("")], usage=usage(100_000, 50)),
            [assistant_message("This retry must not spend more.")],
        ]
    )
    app, run_id = prepare(make_app, model, budget=0.001)
    assert await Compactor(app).compact("main", run_id) is None
    assert len(model.calls) == 1 and app.runs.get(run_id).requests == 1


async def test_successful_retry_records_each_response_once_and_prices_cache_writes(make_app):
    model = ScriptedModel(
        [
            ModelStep(output=[assistant_message("")], usage=usage(2_000, 100, cache_write=500)),
            ModelStep(
                output=[assistant_message("The owner's garden plan is still open.")],
                usage=usage(3_000, 150),
            ),
        ]
    )
    app, run_id = prepare(make_app, model)
    result = await Compactor(app).compact("main", run_id)
    run = app.runs.get(run_id)
    assert result is not None
    assert (run.requests, run.input_tokens, run.output_tokens) == (2, 5_000, 250)
    assert run.cost_usd == pytest.approx(
        cost_usd(
            app.config.model.fast, input_tokens=2_000, output_tokens=100, cache_write_tokens=500
        )
        + cost_usd(app.config.model.fast, input_tokens=3_000, output_tokens=150)
    )


async def test_short_compaction_deadline_leaves_history_and_allows_main_answer(make_app):
    cancelled = asyncio.Event()

    class WaitingSummary:
        async def summarize(self, previous, conversation):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

    model = ScriptedModel([[assistant_message("The current request can still be answered.")]])
    app = make_app(model, config=config(timeout_s=0.02))
    app.summarizer = WaitingSummary()
    seed(app)
    app.inbox.add(owner_message("continue"))
    result = await Worker(app).run_once()
    assert result.outcome.status == "completed" and cancelled.is_set()
    assert app.sessions.summary("main") is None and len(model.calls) == 1


@pytest.mark.parametrize("phase", ["prepare", "compact"])
async def test_whole_run_deadline_includes_work_before_main_model(make_app, monkeypatch, phase):
    cfg = config(timeout_s=10).model_copy(update={"runtime": RuntimeConfig(run_timeout_s=0.03)})
    model = ScriptedModel([[assistant_message("Must not be reached.")]])
    app = make_app(model, config=cfg)
    cancelled = asyncio.Event()

    async def wait_forever(*args, **kwargs):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    if phase == "prepare":
        monkeypatch.setattr(app.mcp, "prepare", wait_forever)
    else:
        monkeypatch.setattr(app.summarizer, "summarize", wait_forever)
        seed(app)
    app.inbox.add(owner_message("continue"))
    await Worker(app).run_once()
    [run] = app.runs.recent(1)
    assert run.status == "failed" and "RunTimedOut" in run.error
    assert cancelled.is_set() and not model.calls
