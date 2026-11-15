"""Approval flow end to end: a risky call suspends the run, the owner answers later (maybe after a
restart), and the run continues with the decision. Nothing executes without a yes, and an
approved action runs at most once."""

from __future__ import annotations

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message, roles, text_of
from personal_agent.config import Config, PolicyConfig
from personal_agent.core.approvals import DENIED_MESSAGE, EXPIRED_MESSAGE, SUPERSEDED_MESSAGE
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.sessions import INTERRUPTED_RESULT
from tooling import Spy, drain, install

HOUR = 3600


def push() -> list:
    return [function_call("shell", {"command": "git push origin main"}, call_id="c1")]


def outputs(app, key="main") -> dict[str, str]:
    return {
        i["call_id"]: i["output"]
        for i in app.sessions.items(key)
        if i.get("type") == "function_call_output"
    }


async def suspended(make_app, model, **kw):
    """An app whose first owner message has led to a run waiting for approval."""
    app = make_app(model, **kw)
    spy = install(app, Spy())
    app.inbox.add(owner_message("please push my branch"))
    [result] = await drain(app)
    return app, spy, result


# ---- suspending ---------------------------------------------------------------------------
async def test_a_risky_call_suspends_the_run_and_asks_the_owner(make_app):
    model = ScriptedModel([push(), [assistant_message("Pushed.")]])
    app, spy, result = await suspended(make_app, model)

    assert result.outcome.status == "awaiting_approval"
    assert spy.ran() == []  # nothing happened yet
    assert app.runs.get(result.outcome.run_id).status == "awaiting_approval"
    assert app.inbox.counts()["done"] == 1  # the worker is free; nothing is blocked

    [approval] = app.approvals.pending()
    assert (approval.tool, approval.summary) == ("shell", "git push origin main")
    [ask] = app.outbox.list()
    assert ask.kind == "approval_request" and f"/approve {approval.id}" in ask.text
    assert ask.payload["approval_ids"] == [approval.id]
    assert len(model.calls) == 1  # the model is not consulted while waiting
    assert "c1" not in outputs(app)  # the call is still open in the log

    [row] = app.audit.recent()
    assert (row.tool, row.verdict, row.layer, row.outcome) == ("shell", "ask", "mode", None)


async def test_approving_continues_the_run_and_runs_the_tool_exactly_once(make_app):
    model = ScriptedModel([push(), [assistant_message("Pushed.")]])
    app, spy, first = await suspended(make_app, model)
    [approval] = app.approvals.pending()

    assert app.approvals.decide(approval.id, True, by="owner").resume_queued
    [second] = await drain(app)

    assert second.outcome.status == "completed" and second.outcome.run_id == first.outcome.run_id
    assert spy.ran("shell") == ["git push origin main"]
    assert outputs(app)["c1"] == "ran: git push origin main"
    items = app.sessions.items("main")
    assert roles(items).count("function_call") == roles(items).count("function_call_output") == 1
    assert text_of(items[-1]) == "Pushed."
    sent = [m.text for m in app.outbox.list() if m.kind == "message"]
    assert sent == ["Pushed."]
    assert app.approvals.load_run(first.outcome.run_id) is None  # state cleaned up
    run = app.runs.get(first.outcome.run_id)
    assert run.status == "completed" and run.requests == 2
    assert app.audit.recent()[0].outcome == "ok"


async def test_denying_tells_the_model_and_nothing_runs(make_app):
    model = ScriptedModel([push(), [assistant_message("Understood, I did not push.")]])
    app, spy, _ = await suspended(make_app, model)
    [approval] = app.approvals.pending()

    app.approvals.decide(approval.id, False, by="owner", note="not on main")
    await drain(app)

    assert spy.ran() == []
    assert DENIED_MESSAGE in outputs(app)["c1"] and "not on main" in outputs(app)["c1"]
    assert DENIED_MESSAGE in str(model.calls[1].input)  # the model saw the refusal
    assert [m.text for m in app.outbox.list() if m.kind == "message"] == [
        "Understood, I did not push."
    ]
    assert app.audit.recent()[0].outcome == "rejected"


async def test_the_answer_can_arrive_after_a_restart(make_app):
    app, spy1, first = await suspended(make_app, ScriptedModel([push()]))
    [approval] = app.approvals.pending()
    app.close()  # the process dies while the owner is away

    app2 = make_app(ScriptedModel([[assistant_message("Pushed.")]]))
    spy2 = install(app2, Spy())
    app2.recover()
    assert app2.approvals.pending()[0].id == approval.id  # still waiting
    app2.approvals.decide(approval.id, True, by="owner")
    [result] = await drain(app2)

    assert result.outcome.status == "completed"
    assert spy1.ran() == [] and spy2.ran("shell") == ["git push origin main"]
    assert app2.runs.get(first.outcome.run_id).status == "completed"


async def test_an_unanswered_request_expires_and_the_run_hears_about_it(make_app, clock):
    model = ScriptedModel([push(), [assistant_message("No answer, so I stopped.")]])
    app, spy, _ = await suspended(make_app, model)

    clock.advance(13 * HOUR)
    assert [a.status for a in app.approvals.expire_due()] == ["expired"]
    await drain(app)

    assert spy.ran() == []
    assert outputs(app)["c1"].startswith(EXPIRED_MESSAGE)
    assert app.audit.recent()[0].outcome == "expired"


async def test_a_new_message_supersedes_the_waiting_request(make_app):
    model = ScriptedModel([push(), [assistant_message("Sure, what instead?")]])
    app, spy, first = await suspended(make_app, model)
    [approval] = app.approvals.pending()

    app.inbox.add(owner_message("actually, never mind"))
    await drain(app)

    items = app.sessions.items("main")
    assert roles(items) == ["user", "function_call", "function_call_output", "user", "assistant"]
    assert outputs(app)["c1"] == SUPERSEDED_MESSAGE
    assert spy.ran() == []
    assert app.runs.get(first.outcome.run_id).status == "cancelled"
    assert app.approvals.get(approval.id).status == "superseded"
    late = app.approvals.decide(approval.id, True, by="owner")
    assert not late.changed and not late.resume_queued
    assert app.approvals.load_run(first.outcome.run_id) is None


async def test_a_background_event_does_not_disturb_a_waiting_run(make_app):
    model = ScriptedModel(
        [push(), [assistant_message("Nothing to report.")], [assistant_message("Pushed.")]]
    )
    app, spy, first = await suspended(make_app, model)
    [approval] = app.approvals.pending()

    # A timer fires into the same conversation while the owner is away.
    app.timers.add_followup(
        name="later", prompt="check in", due_at=app.clock.now() + 10, session_key="main"
    )
    app.clock.advance(11)
    app.timers.tick()
    await drain(app)

    assert "c1" not in outputs(app)  # the open call was not "repaired" with a fake result
    assert app.approvals.get(approval.id).status == "pending"

    app.approvals.decide(approval.id, True, by="owner")
    await drain(app)
    assert spy.ran("shell") == ["git push origin main"]
    assert outputs(app)["c1"] == "ran: git push origin main"
    assert app.runs.get(first.outcome.run_id).status == "completed"


async def test_several_calls_in_one_step_need_every_answer_before_the_run_resumes(make_app):
    two = [
        function_call("shell", {"command": "make deploy"}, call_id="a"),
        function_call("shell", {"command": "rm -rf build"}, call_id="b"),
    ]
    model = ScriptedModel([two, [assistant_message("Deployed; I left build/ alone.")]])
    app, spy, _ = await suspended(make_app, model)
    first, second = app.approvals.pending()

    assert not app.approvals.decide(first.id, True, by="owner").resume_queued
    assert await drain(app) == []  # still waiting for the second answer
    assert spy.ran() == []

    assert app.approvals.decide(second.id, False, by="owner").resume_queued
    await drain(app)

    assert spy.ran("shell") == ["make deploy"]
    assert outputs(app)["a"] == "ran: make deploy"
    assert outputs(app)["b"].startswith(DENIED_MESSAGE)


async def test_a_resumed_run_can_ask_again(make_app):
    again = [function_call("shell", {"command": "git tag v1"}, call_id="c2")]
    model = ScriptedModel([push(), again, [assistant_message("Pushed and tagged.")]])
    app, spy, first = await suspended(make_app, model)

    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")
    [mid] = await drain(app)
    assert mid.outcome.status == "awaiting_approval" and mid.outcome.run_id == first.outcome.run_id
    assert spy.ran() == ["git push origin main"]

    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")
    [last] = await drain(app)
    assert last.outcome.status == "completed"
    assert spy.ran() == ["git push origin main", "git tag v1"]
    assert app.runs.get(first.outcome.run_id).requests == 3


async def test_approval_state_keeps_taint_and_origin_across_the_suspension(make_app):
    app, _, first = await suspended(make_app, ScriptedModel([push()]))
    saved = app.approvals.load_run(first.outcome.run_id)
    assert saved.meta["origin"] == {"channel": "cli", "recipient": None, "interactive": True}
    assert saved.meta["owner_request"] == "please push my branch"
    assert saved.meta["profile"] == "full" and saved.meta["tainted"] is False


# ---- at most once ---------------------------------------------------------------------------
async def test_a_crash_while_an_approved_action_ran_never_repeats_it(make_app):
    model = ScriptedModel([push(), [assistant_message("I could not confirm the push.")]])
    app, spy, first = await suspended(make_app, model)
    [approval] = app.approvals.pending()
    app.approvals.decide(approval.id, True, by="owner")

    # The action started, then the process died before its result was recorded.
    app.audit.set_outcome(first.outcome.run_id, "c1", "running")
    assert app.recover()["interrupted_tool_calls"] == 1
    await drain(app)

    assert spy.ran() == []  # not executed a second time
    assert outputs(app)["c1"] == INTERRUPTED_RESULT


async def test_a_run_that_cannot_be_resumed_is_dropped_cleanly_after_repeated_failure(
    make_app, clock
):
    model = ScriptedModel(
        [push(), RuntimeError("boom"), RuntimeError("boom"), RuntimeError("boom")]
    )
    app, _spy, first = await suspended(make_app, model)
    app.approvals.decide(app.approvals.pending()[0].id, True, by="owner")

    for _ in range(6):
        await drain(app)
        clock.advance(2000)
        if app.approvals.load_run(first.outcome.run_id) is None:
            break

    assert app.approvals.load_run(first.outcome.run_id) is None
    assert app.runs.get(first.outcome.run_id).status == "failed"
    assert any("could not continue a task" in m.text for m in app.outbox.list())
    assert app.inbox.counts()["dead"] == 1


async def test_resume_events_for_unknown_runs_are_ignored(make_app):
    app = make_app(ScriptedModel([]))
    app.inbox.add(
        NewEvent(
            source="approval",
            kind=Kind.APPROVAL_RESPONSE,
            session_key="main",
            payload={"run_id": "nope"},
        )
    )
    [result] = await drain(app)
    assert result.outcome.status == "silent" and app.inbox.counts()["done"] == 1


# ---- the profile decides who may ask ------------------------------------------------------
async def test_a_scheduled_run_asks_through_the_default_channel(make_app, clock):
    model = ScriptedModel([push()])
    app = make_app(model)
    install(app, Spy())
    app.timers.add_followup(name="deploy", prompt="Deploy now.", due_at=clock.now() + 5)
    clock.advance(6)
    app.timers.tick()
    [result] = await drain(app)

    assert result.outcome.status == "awaiting_approval"
    [ask] = [m for m in app.outbox.list() if m.kind == "approval_request"]
    assert ask.channel == "cli"
    assert app.approvals.load_run(result.outcome.run_id).meta["profile"] == "scheduled"


async def test_autonomous_mode_never_asks(make_app):
    cfg = Config(policy=PolicyConfig(mode="autonomous", i_understand_autonomous_risk=True))
    model = ScriptedModel([push(), [assistant_message("Pushed.")]])
    _app, spy, result = await suspended(make_app, model, config=cfg)
    assert result.outcome.status == "completed" and spy.ran("shell") == ["git push origin main"]
