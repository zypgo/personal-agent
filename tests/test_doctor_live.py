"""`pa doctor --live` without the network: scripted turns stand in for the real model.

The real thing (two turns against OpenAI) is tested in tests/live; these tests are about what the
check *does* with every way a turn can end, because a self-test that crashes on the first odd
answer is worse than none.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import invoke
from personal_agent.core.worker import Worker, WorkResult
from personal_agent.doctor import LIVE_PROMPTS, live_checks
from personal_agent.harness.runner import RunOutcome


def view_memory():
    return function_call("memory", {"command": "view", "path": "/memories"}, call_id="c1")


def summary(checks):
    return [(c.name, c.status) for c in checks]


async def test_two_good_turns_prove_the_key_the_tools_and_the_history_replay(make_app):
    model = ScriptedModel(
        [[view_memory()], [assistant_message("READY")], [assistant_message("You ran a self-test.")]]
    )
    app = make_app(model)

    checks = await live_checks(app)

    assert summary(checks) == [
        ("live turn 1", "ok"),
        ("live turn 2", "ok"),
        ("history replay", "ok"),
    ]
    assert "2 model calls, 1 tool calls" in checks[0].detail
    # the second turn really did carry the first one's history back to the model
    assert LIVE_PROMPTS[0] in str(model.calls[-1].input)


async def test_the_self_test_leaves_no_session_and_no_message_behind(make_app):
    app = make_app(
        ScriptedModel([[view_memory()], [assistant_message("READY")], [assistant_message("ok")]])
    )
    session = f"doctor-{int(app.clock.now())}"

    await live_checks(app)

    assert app.sessions.items(session) == []
    assert not [m for m in app.outbox.recent(status="pending") if m.session_key == session]


async def test_a_model_that_skips_the_memory_tool_is_a_warning_not_a_failure(make_app):
    app = make_app(ScriptedModel([[assistant_message("READY")], [assistant_message("ok")]]))

    checks = await live_checks(app)

    assert summary(checks) == [
        ("live turn 1", "warn"),
        ("live turn 2", "ok"),
        ("history replay", "ok"),
    ]
    assert "did not call the memory tool" in checks[0].detail


async def test_a_turn_that_fails_stops_the_test_with_the_reason(make_app):
    model = ScriptedModel([RuntimeError("the API said no")])
    app = make_app(model)

    checks = await live_checks(app)

    assert summary(checks) == [("live turn 1", "fail")]
    assert len(model.calls) == 1  # no second turn on top of a broken first one


async def test_a_second_turn_that_fails_is_reported_as_the_replay_problem(make_app):
    app = make_app(
        ScriptedModel([[view_memory()], [assistant_message("READY")], RuntimeError("x")])
    )

    checks = await live_checks(app)

    assert summary(checks) == [("live turn 1", "ok"), ("live turn 2", "fail")]


async def test_a_run_that_left_no_record_is_a_failure_not_a_crash(make_app, monkeypatch):
    """`runs.get` can come back empty; the check used to dereference it."""
    app = make_app(ScriptedModel([[assistant_message("READY")]]))
    monkeypatch.setattr(app.runs, "get", lambda run_id: None)

    checks = await live_checks(app)

    assert summary(checks) == [("live turn 1", "fail")]
    assert "no record" in checks[0].detail


async def test_a_run_that_ends_without_completing_is_a_failure(make_app, monkeypatch):
    app = make_app()

    async def run_once(self, **kwargs):
        return WorkResult(batch=None, outcome=RunOutcome("r1", "awaiting_approval"))

    monkeypatch.setattr(Worker, "run_once", run_once)
    monkeypatch.setattr(
        app.runs, "get", lambda run_id: SimpleNamespace(cost_usd=0.01, requests=1, tool_calls=0)
    )

    checks = await live_checks(app)

    assert summary(checks) == [("live turn 1", "fail")]
    assert "awaiting_approval" in checks[0].detail


async def test_no_run_at_all_is_a_failure_that_says_so(make_app, monkeypatch):
    app = make_app()

    async def run_once(self, **kwargs):
        return None

    monkeypatch.setattr(Worker, "run_once", run_once)

    checks = await live_checks(app)

    assert summary(checks) == [("live turn 1", "fail")]
    assert checks[0].detail == "no run happened"


@pytest.mark.parametrize("turn", [0, 1])
async def test_the_cost_of_each_turn_is_reported(make_app, turn):
    app = make_app(
        ScriptedModel([[view_memory()], [assistant_message("READY")], [assistant_message("ok")]])
    )

    checks = await live_checks(app)

    assert "about $" in checks[turn].detail


def _a_doctor_that_counts_how_often_the_services_are_stopped(home, monkeypatch, wired):
    invoke("init")  # with the real wiring; the scripted app takes over afterwards
    monkeypatch.setenv("OPENAI_API_KEY", "sk-" + "test-not-real")
    app = wired(
        ScriptedModel([[view_memory()], [assistant_message("READY")], [assistant_message("ok")]])
    )
    stopped: list[bool] = []
    stop = app.stop_services

    async def counting() -> None:
        stopped.append(True)
        await stop()

    monkeypatch.setattr(app, "stop_services", counting)
    return stopped


def test_the_command_closes_what_the_check_opened_before_the_loop_ends(home, monkeypatch, wired):
    stopped = _a_doctor_that_counts_how_often_the_services_are_stopped(home, monkeypatch, wired)

    result = invoke("doctor", "--live")

    assert result.exit_code == 0, result.output
    assert "history replay" in result.output
    assert stopped == [True]


def test_they_are_closed_even_when_the_check_itself_breaks(home, monkeypatch, wired):
    from personal_agent.commands import setup

    stopped = _a_doctor_that_counts_how_often_the_services_are_stopped(home, monkeypatch, wired)

    async def broken(_app):
        raise RuntimeError("the check broke")

    monkeypatch.setattr(setup, "live_checks", broken)

    result = invoke("doctor", "--live")

    assert isinstance(result.exception, RuntimeError)
    assert stopped == [True]
