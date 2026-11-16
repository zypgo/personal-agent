"""Recovery across state transitions, not just the happy path of each primitive."""

from __future__ import annotations

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message
from personal_agent.clock import get_tz
from personal_agent.config import BudgetConfig
from personal_agent.core.budget import BudgetExceeded, BudgetGuard
from personal_agent.core.runs import RunStore
from personal_agent.core.worker import Worker
from personal_agent.harness.runner import Harness
from tooling import Spy, install


class ProcessStopped(BaseException):
    """Application Exception handlers must not mistake a process death for a tool failure."""


def shell(command, call_id):
    return [function_call("shell", {"command": command}, call_id=call_id)]


@pytest.mark.parametrize("exhausted", [False, True])
async def test_old_resume_event_cannot_consume_the_next_approval_round(make_app, exhausted):
    app = make_app(
        ScriptedModel(
            [
                shell("first", "c1"),
                shell("second", "c2"),
                [assistant_message("Done")],
            ]
        )
    )
    spy = install(app, Spy())
    app.inbox.add(owner_message("Do two actions"))
    first = await Worker(app).run_once()
    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")
    batch = app.inbox.claim("before-crash")
    assert (await Harness(app).run(batch)).status == "awaiting_approval"
    assert spy.ran() == ["first"]

    # The second suspension committed; the first resume event has not been acknowledged.
    if exhausted:
        app.inbox.max_attempts = 1  # even terminal settlement must respect the generation
    app.recover()
    await Worker(app).run_once()
    assert app.runs.get(first.outcome.run_id).status == "awaiting_approval"
    assert app.approvals.load_run(first.outcome.run_id) is not None
    waiting = app.approvals.pending()
    assert len(waiting) == 1 and waiting[0].call_id == "c2"
    assert app.approvals.decide(waiting[0].id, True, by="owner").resume_queued
    await Worker(app).run_once()
    assert spy.ran() == ["first", "second"]
    assert app.runs.get(first.outcome.run_id).status == "completed"


@pytest.mark.parametrize("operation", ["supersede_session", "drop_run"])
async def test_supersede_commits_every_transition_or_none(make_app, monkeypatch, operation):
    app = make_app(ScriptedModel([shell("first", "c1"), [assistant_message("New task")]]))
    install(app, Spy())
    app.inbox.add(owner_message("Do action"))
    first = await Worker(app).run_once()
    original = getattr(app.approvals, operation)

    def stop(*args, **kwargs):
        original(*args, **kwargs)
        raise ProcessStopped()

    monkeypatch.setattr(app.approvals, operation, stop)
    app.inbox.add(owner_message("Do something else instead"))
    with pytest.raises(ProcessStopped):
        await Worker(app).run_once()
    assert len(app.approvals.pending()) == 1  # the entire supersede rolled back
    monkeypatch.setattr(app.approvals, operation, original)
    app.recover()
    await Worker(app).run_once()
    assert app.approvals.load_run(first.outcome.run_id) is None
    assert app.runs.get(first.outcome.run_id).status == "cancelled"
    outputs = [i for i in app.sessions.items("main") if i.get("type") == "function_call_output"]
    assert [i["call_id"] for i in outputs] == ["c1"]


async def test_superseding_round_two_does_not_duplicate_round_one_results(make_app):
    app = make_app(
        ScriptedModel(
            [
                shell("first", "c1"),
                shell("second", "c2"),
                [assistant_message("Changed task")],
            ]
        )
    )
    install(app, Spy())
    app.inbox.add(owner_message("Two actions"))
    await Worker(app).run_once()
    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")
    await Worker(app).run_once()
    app.inbox.add(owner_message("Cancel and change task"))
    await Worker(app).run_once()
    outputs = [
        i["call_id"] for i in app.sessions.items("main") if i.get("type") == "function_call_output"
    ]
    assert outputs == ["c1", "c2"]


async def test_new_message_repairs_a_supersede_half_committed_by_an_old_version(make_app):
    app = make_app(ScriptedModel([shell("first", "c1"), [assistant_message("Changed task")]]))
    install(app, Spy())
    app.inbox.add(owner_message("Do action"))
    first = await Worker(app).run_once()
    app.approvals.supersede_session("main")  # the pre-fix crash left exactly this state
    app.inbox.add(owner_message("Continue with my new task"))
    await Worker(app).run_once()
    assert app.approvals.load_run(first.outcome.run_id) is None
    assert app.runs.get(first.outcome.run_id).status == "cancelled"
    assert any(
        i.get("call_id") == "c1" and i.get("type") == "function_call_output"
        for i in app.sessions.items("main")
    )


def test_usage_after_midnight_is_charged_to_its_actual_day(db, clock):
    clock.set(1_700_000_000)  # 22:13 UTC, within the default 12-hour approval lifetime.
    runs = RunStore(db, clock)
    budget = BudgetGuard(
        runs, db, BudgetConfig(daily_usd=0.1, per_run_usd=1), clock=clock, tz=get_tz("UTC")
    )
    run_id = runs.start(
        session_key="main", trigger="message", profile="full", model="fake", event_ids=[]
    )
    runs.add_usage(run_id, cost_usd=0.01)
    runs.set_status(run_id, "awaiting_approval")
    clock.advance(7200)
    runs.add_usage(run_id, cost_usd=0.50)
    assert budget.spent_today() == pytest.approx(0.50)
    assert runs.get(run_id).cost_usd == pytest.approx(0.51)
    with pytest.raises(BudgetExceeded, match="daily"):
        budget.check_run(run_id)


def test_daily_usage_survives_deleting_the_run_summary(db, clock):
    runs = RunStore(db, clock)
    run_id = runs.start(
        session_key="main", trigger="message", profile="full", model="fake", event_ids=[]
    )
    runs.add_usage(run_id, cost_usd=0.5)
    db.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    assert runs.cost_since(clock.now() - 1) == 0.5


async def test_final_failure_notifies_without_another_ready_event(make_app, clock):
    app = make_app(ScriptedModel([RuntimeError("bug")] * 4))
    app.inbox.add(owner_message("Please help"))
    worker = Worker(app)
    for _ in range(4):
        assert app.inbox.has_ready()
        await worker.run_once()
        clock.advance(601)
    assert not app.inbox.has_ready()
    assert app.inbox.counts()["dead"] == 1
    assert any("gave up" in message.text for message in app.outbox.recent())
    assert not app.inbox.has_unreported_dead()


async def test_dead_notification_and_its_ack_commit_together(make_app, monkeypatch):
    app = make_app()
    app.inbox.max_attempts = 1
    app.inbox.add(owner_message("Please help"))
    batch = app.inbox.claim("dead-worker")
    app.inbox.fail(batch, "bug")
    worker = Worker(app)
    original = worker._tell_owner_about_dead

    def stop(events):
        original(events)
        raise ProcessStopped()

    monkeypatch.setattr(worker, "_tell_owner_about_dead", stop)
    with pytest.raises(ProcessStopped):
        await worker.run_once()
    assert app.inbox.has_unreported_dead()
    assert app.outbox.recent() == []
    app.recover()
    await Worker(app).run_once()
    notices = [m for m in app.outbox.recent() if "gave up" in m.text]
    assert len(notices) == 1
    assert not app.inbox.has_unreported_dead()
