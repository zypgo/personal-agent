from __future__ import annotations

import pytest
from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from helpers import owner_message, usage
from personal_agent.clock import get_tz
from personal_agent.config import BudgetConfig, Config
from personal_agent.core.budget import BudgetExceeded, BudgetGuard
from personal_agent.core.runs import RunStore
from tooling import drain

# gpt-6.1-sol: $2 / 1M input, $10 / 1M output -> 1000 in + 100 out = $0.003 per model call
CALL_COST = 0.003


def spend(runs: RunStore, session: str, cost: float):
    run_id = runs.start(
        session_key=session, trigger="message", profile="full", model="m", event_ids=[]
    )
    runs.add_usage(run_id, cost_usd=cost)
    return run_id


@pytest.fixture
def guard(db, clock):
    runs = RunStore(db, clock)
    return runs, BudgetGuard(
        runs, db, BudgetConfig(per_run_usd=1.0, daily_usd=5.0), clock=clock, tz=get_tz("UTC")
    )


def call(i: int):
    return function_call("memory", {"command": "view", "path": "/memories"}, call_id=f"c{i}")


def costly(*output) -> ModelStep:
    """A model step that costs $0.003."""
    return ModelStep(output=list(output), usage=usage(1000, 100))


def big(*output) -> ModelStep:
    """$0.005: over a $0.004 daily cap by itself."""
    return ModelStep(output=list(output), usage=usage(2000, 100))


def tiny(text: str) -> ModelStep:
    """$0.0003."""
    return ModelStep(output=[assistant_message(text)], usage=usage(100, 10))


DAILY = BudgetConfig(per_run_usd=1.0, daily_usd=0.004)


# ---- the guard on its own -----------------------------------------------------------------
def test_spending_below_both_limits_is_fine(guard):
    runs, budget = guard
    run_id = spend(runs, "main", 0.5)
    budget.check_run(run_id)
    budget.check_day()
    status = budget.status()
    assert (status.spent_today, status.daily_limit, status.remaining_today) == (0.5, 5.0, 4.5)


def test_a_run_over_its_cap_raises_with_the_run_scope(guard):
    runs, budget = guard
    run_id = spend(runs, "main", 1.0)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check_run(run_id)
    assert caught.value.scope == "run" and caught.value.limit == 1.0


def test_a_tighter_cap_for_one_run_applies_before_the_general_one(guard):
    runs, budget = guard
    run_id = spend(runs, "main", 0.4)
    budget.check_run(run_id)  # under the general $1.00
    budget.check_run(run_id, 0.5)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check_run(run_id, 0.3)
    assert caught.value.scope == "run" and caught.value.limit == 0.3


def test_a_cap_for_one_run_cannot_lift_the_general_one(guard):
    runs, budget = guard
    run_id = spend(runs, "main", 1.0)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check_run(run_id, 50.0)
    assert caught.value.limit == 1.0


def test_the_day_cap_counts_every_session_and_says_when_it_resets(guard):
    runs, budget = guard
    spend(runs, "a", 2.5)
    spend(runs, "b", 2.5)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check_day()
    assert caught.value.scope == "daily"
    assert 0 < caught.value.resets_in_s <= 86400  # until the next local midnight


def test_yesterdays_spending_does_not_count_today(guard, clock):
    runs, budget = guard
    spend(runs, "a", 6.0)
    clock.advance(86400)
    budget.check_day()
    assert budget.spent_today() == 0.0


def test_extending_today_raises_only_todays_limit(guard, clock):
    runs, budget = guard
    spend(runs, "a", 5.0)
    with pytest.raises(BudgetExceeded):
        budget.check_day()
    assert budget.extend_today(2.0) == 7.0
    budget.check_day()
    clock.advance(86400)
    assert budget.daily_limit() == 5.0  # tomorrow is back to the configured cap
    with pytest.raises(ValueError):
        budget.extend_today(0)


# ---- inside runs ------------------------------------------------------------------------------
async def test_the_per_run_cap_stops_a_runaway_run_and_tells_the_owner(make_app):
    model = ScriptedModel([costly(call(i)) for i in range(10)])
    app = make_app(model, config=Config(budget=BudgetConfig(per_run_usd=0.005, daily_usd=5.0)))
    app.inbox.add(owner_message("loop"))
    [result] = await drain(app)

    assert result.outcome.status == "failed"
    # Checked before every call: the third call is refused once two calls cost $0.006.
    assert len(model.calls) == 2
    run = app.runs.get(result.outcome.run_id)
    assert run.status == "failed" and "run budget reached" in run.error
    [notice] = app.outbox.recent()
    assert "cost limit" in notice.text and "$0.01" in notice.text
    assert app.inbox.counts()["done"] == 1  # not retried: a retry would spend the same again


async def test_an_answer_that_was_paid_for_is_never_thrown_away(make_app):
    model = ScriptedModel([big(assistant_message("Here is your answer."))])
    app = make_app(model, config=Config(budget=DAILY))
    app.inbox.add(owner_message("question"))
    [result] = await drain(app)
    assert result.outcome.status == "completed"
    assert [m.text for m in app.outbox.recent()] == ["Here is your answer."]


async def test_the_daily_cap_pauses_work_until_midnight_and_nothing_is_lost(make_app, clock):
    model = ScriptedModel([big(call(0)), tiny("first"), tiny("second")])
    app = make_app(model, config=Config(budget=DAILY))
    app.inbox.add(owner_message("one", session="a"))
    [first] = await drain(app)

    assert first.failure is not None and first.failure.environmental
    assert app.runs.recent()[0].status == "paused_budget"
    [notice] = app.outbox.recent()
    assert "Today's budget" in notice.text and "pa budget extend" in notice.text
    event = app.inbox.get(1)
    assert event["status"] == "pending" and event["attempts"] == 0  # waiting, not failing

    # More work arrives while paused: no model call, no new run record, no second notice.
    app.inbox.add(owner_message("two", session="b"))
    await drain(app)
    assert len(model.calls) == 1 and len(app.runs.recent()) == 1
    assert len(app.outbox.recent()) == 1

    # After local midnight everything proceeds.
    clock.advance(86_400)
    results = await drain(app)
    assert {r.outcome.status for r in results} == {"completed"}
    assert app.inbox.counts()["done"] == 2 and app.inbox.counts()["dead"] == 0
    assert sorted(m.text for m in app.outbox.recent() if m.kind == "message") == ["first", "second"]


async def test_raising_todays_cap_releases_the_waiting_events_immediately(make_app):
    model = ScriptedModel([big(call(0)), tiny("again")])
    app = make_app(model, config=Config(budget=DAILY))
    app.inbox.add(owner_message("one"))
    await drain(app)
    assert await drain(app) == []  # still paused

    limit, released = app.extend_budget(1.0)
    assert (limit, released) == (1.004, 1)
    [result] = await drain(app)
    assert result.outcome.status == "completed"
    assert any(m.text == "again" for m in app.outbox.recent())


async def test_a_paused_run_keeps_its_cost_on_the_record(make_app):
    model = ScriptedModel([big(call(0))])
    app = make_app(model, config=Config(budget=DAILY))
    app.inbox.add(owner_message("one"))
    await drain(app)
    run = app.runs.recent()[0]
    assert run.requests == 1 and abs(run.cost_usd - 0.005) < 1e-9


# ---- money that is not a model run (speech-to-text) ---------------------------------------------
def test_spend_outside_runs_counts_toward_today(guard, clock):
    runs, budget = guard
    spend(runs, "main", 1.0)
    budget.record_spend("transcription", 0.5, "voice 60s")
    assert budget.spent_today() == pytest.approx(1.5)
    assert budget.status().spent_today == pytest.approx(1.5)
    assert budget.status().remaining_today == pytest.approx(3.5)


def test_spend_from_yesterday_is_not_counted_today(guard, clock):
    _, budget = guard
    budget.record_spend("transcription", 2.0)
    clock.advance(86_400 * 2)
    assert budget.spent_today() == 0.0
    assert budget.spend_since(0) == pytest.approx(2.0)  # still on the record


def test_enough_spend_outside_runs_pauses_the_agent_like_any_other(guard):
    _, budget = guard
    budget.record_spend("transcription", 5.01)
    with pytest.raises(BudgetExceeded) as caught:
        budget.check_day()
    assert caught.value.scope == "daily"


def test_negative_or_zero_spend_is_not_recorded(guard):
    _, budget = guard
    budget.record_spend("transcription", 0.0)
    budget.record_spend("transcription", -1.0)
    assert budget.spend_since(0) == 0.0
