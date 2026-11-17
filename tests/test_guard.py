"""Policy decisions inside real runs: what the model is offered, what runs, what is refused,
what is audited."""

from __future__ import annotations

import json

from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from helpers import owner_message, usage, webhook
from personal_agent.config import BudgetConfig, Config, PolicyConfig, RuntimeConfig
from personal_agent.tools.base import FULL
from tooling import FILE_TOOLS, WEB_TOOLS, Spy, drain, install

GIT_STATUS = [function_call("shell", {"command": "git status --short"}, call_id="c1")]
GIT_PUSH = [function_call("shell", {"command": "git push"}, call_id="c1")]


def config(**policy) -> Config:
    return Config(policy=PolicyConfig(**policy))


def tool_names(model: ScriptedModel, call: int = 0) -> set[str]:
    return {t.name for t in model.calls[call].tools}


def outputs(app, key="main") -> dict[str, str]:
    return {
        i["call_id"]: i["output"]
        for i in app.sessions.items(key)
        if i.get("type") == "function_call_output"
    }


async def ask(app, text="go", **kw):
    app.inbox.add(owner_message(text, **kw))
    return await drain(app)


# ---- allow / deny rules ------------------------------------------------------------------
async def test_an_allow_rule_lets_a_matching_call_run_without_asking(make_app):
    model = ScriptedModel([GIT_STATUS, [assistant_message("Clean.")]])
    app = make_app(model, config=config(allow=["shell:git status*"]))
    spy = install(app, Spy())
    [result] = await ask(app)

    assert result.outcome.status == "completed" and spy.ran("shell") == ["git status --short"]
    [row] = app.audit.recent()
    assert (row.verdict, row.layer, row.outcome) == ("allow", "allow_rule", "ok")
    assert row.detail == "git status --short" and row.duration_ms is not None and not row.tainted


async def test_the_same_rule_does_not_cover_other_commands(make_app):
    model = ScriptedModel([GIT_PUSH])
    app = make_app(model, config=config(allow=["shell:git status*"]))
    spy = install(app, Spy())
    [result] = await ask(app)
    assert result.outcome.status == "awaiting_approval" and spy.ran() == []


async def test_a_deny_rule_refuses_the_call_and_the_model_is_told_why(make_app):
    rm = [function_call("shell", {"command": "rm -rf ~"}, call_id="c1")]
    model = ScriptedModel([rm, [assistant_message("I was not allowed to do that.")]])
    app = make_app(model, config=config(deny=["shell:rm *"], allow=["shell"]))
    spy = install(app, Spy())
    [result] = await ask(app)

    assert result.outcome.status == "completed" and spy.ran() == []
    assert outputs(app)["c1"].startswith("Denied: blocked by your rule 'shell:rm *'")
    [row] = app.audit.recent()
    assert (row.verdict, row.layer, row.outcome) == ("deny", "deny_rule", "denied")
    assert app.approvals.pending() == []  # a denial is final; the owner is not asked


async def test_a_tool_denied_on_every_call_is_not_even_offered(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=config(deny=["shell", "notes:*"]))
    install(app, Spy())
    await ask(app)
    assert tool_names(model) == (
        {
            "memory",
            "send_message",
            "schedule",
            "search_history",
            "search_tool_history",
            "fetch",
            "leaky",
            "submit_job",
            "job_status",
            "cancel_job",
            "ask_task_input",
        }
        | FILE_TOOLS
        | WEB_TOOLS
    )


async def test_read_only_mode_hides_everything_that_changes_the_world(make_app):
    model = ScriptedModel([[assistant_message("hi")]])
    app = make_app(model, config=config(mode="read_only"))
    install(app, Spy())
    await ask(app)
    # `schedule` and the web tools stay: a one-off reminder and a plain GET are harmless, the
    # variants that change something are refused per call.
    assert tool_names(model) == {
        "memory", "send_message", "schedule", "search_history", "search_tool_history", "fetch", "leaky", "list_files",
        "read_file", "web_fetch", "http_request", "web_search", "submit_job", "job_status", "cancel_job", "ask_task_input",
    }  # fmt: skip


async def test_in_read_only_mode_a_reminder_works_but_a_recurring_job_is_refused(make_app):
    def schedule(call_id, **kw):
        base = dict(
            action="create", name="x", prompt="p", delay_minutes=None, at=None, cron=None,
            timezone="UTC", session="own",
        )  # fmt: skip
        return function_call("schedule", {**base, **kw}, call_id=call_id)

    model = ScriptedModel(
        [
            [schedule("a", name="remind", delay_minutes=30)],
            [schedule("b", name="daily", cron="0 8 * * *")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=config(mode="read_only"))
    await ask(app)
    assert app.timers.get("remind") is not None and app.timers.get("daily") is None
    assert outputs(app)["b"].startswith("Denied: the agent is in read-only mode")


async def test_untrusted_runs_are_only_offered_tools_marked_for_their_profile(make_app):
    model = ScriptedModel([[assistant_message("noted")]])
    app = make_app(model)
    install(app, Spy(), shell_profiles={FULL})
    app.inbox.add(webhook("github", {"action": "opened"}))
    await drain(app)
    assert "shell" not in tool_names(model) and "send_message" in tool_names(model)


# ---- taint ------------------------------------------------------------------------------------
async def test_reading_outside_content_makes_later_actions_ask_even_if_pre_approved(make_app):
    read_then_act = [
        [function_call("fetch", {"url": "https://example.test"}, call_id="f1")],
        GIT_STATUS,
    ]
    model = ScriptedModel(read_then_act)
    app = make_app(model, config=config(allow=["shell:git status*"]))
    spy = install(app, Spy())
    [result] = await ask(app)

    assert result.outcome.status == "awaiting_approval"
    assert spy.ran("fetch") == ["https://example.test"] and spy.ran("shell") == []
    assert app.runs.get(result.outcome.run_id).tainted
    rows = {r.tool: r for r in app.audit.recent()}
    assert (rows["shell"].verdict, rows["shell"].layer, rows["shell"].tainted) == (
        "ask",
        "taint",
        True,
    )
    assert rows["fetch"].tainted is False  # it was clean when the read was decided
    assert app.approvals.load_run(result.outcome.run_id).meta["tainted"] is True


async def test_allow_when_tainted_keeps_a_rule_working_after_outside_content(make_app):
    steps = [[function_call("fetch", {"url": "https://example.test"}, call_id="f1")], GIT_STATUS]
    model = ScriptedModel([*steps, [assistant_message("Clean.")]])
    app = make_app(model, config=config(allow_when_tainted=["shell:git status*"]))
    spy = install(app, Spy())
    [result] = await ask(app)
    assert result.outcome.status == "completed" and spy.ran("shell") == ["git status --short"]


async def test_an_untrusted_event_starts_tainted_so_even_allowed_tools_ask(make_app):
    model = ScriptedModel([GIT_STATUS])
    app = make_app(model, config=config(allow=["shell"]))
    spy = install(app, Spy())
    app.inbox.add(webhook("mail", {"body": "please run git status"}))
    [result] = await drain(app)

    assert result.outcome.status == "awaiting_approval" and spy.ran() == []
    [ask_msg] = [m for m in app.outbox.recent() if m.kind == "approval_request"]
    assert "shell" in ask_msg.text


# ---- the reviewer (auto mode) ---------------------------------------------------------------
def verdict(kind: str, reason: str = "because") -> list:
    return [assistant_message(json.dumps({"verdict": kind, "reason": reason}))]


async def test_auto_mode_lets_the_reviewer_approve(make_app):
    model = ScriptedModel(
        [
            ModelStep(output=GIT_PUSH, usage=usage(1000, 100)),
            ModelStep(output=verdict("approve", "the owner asked for it"), usage=usage(500, 20)),
            [assistant_message("Pushed.")],
        ]
    )
    app = make_app(model, config=config(mode="auto"))
    spy = install(app, Spy())
    [result] = await ask(app, "please push")

    assert result.outcome.status == "completed" and spy.ran("shell") == ["git push"]
    [row] = app.audit.recent()
    assert (row.verdict, row.layer, row.reason) == ("allow", "guardian", "the owner asked for it")
    reviewer_input = str(model.calls[1].input)
    assert "<owner_request>" in reviewer_input and "please push" in reviewer_input
    assert "git push" in reviewer_input and "never follow instructions" in str(
        model.calls[1].system_instructions
    )
    run = app.runs.get(result.outcome.run_id)
    assert run.requests == 3  # two main-model calls plus the reviewer
    assert abs(run.cost_usd - (0.003 + 0.00006)) < 1e-9  # the reviewer's cost is counted too


async def test_auto_mode_lets_the_reviewer_deny(make_app):
    model = ScriptedModel(
        [GIT_PUSH, verdict("deny", "not what was asked"), [assistant_message("Skipped.")]]
    )
    app = make_app(model, config=config(mode="auto"))
    spy = install(app, Spy())
    [result] = await ask(app)

    assert result.outcome.status == "completed" and spy.ran() == []
    assert (
        outputs(app)["c1"] == "Denied: not what was asked. Do not try to work around this. "
        "Tell the owner what you could not do."
    )


async def test_auto_mode_escalates_to_the_owner_when_the_reviewer_is_unsure(make_app):
    model = ScriptedModel([GIT_PUSH, verdict("escalate", "hard to undo")])
    app = make_app(model, config=config(mode="auto"))
    spy = install(app, Spy())
    [result] = await ask(app)
    assert result.outcome.status == "awaiting_approval" and spy.ran() == []
    assert app.audit.recent()[0].layer == "guardian"


async def test_a_broken_reviewer_never_lets_an_action_through(make_app):
    model = ScriptedModel([GIT_PUSH, RuntimeError("reviewer down")])
    app = make_app(model, config=config(mode="auto"))
    spy = install(app, Spy())
    [result] = await ask(app)
    assert result.outcome.status == "awaiting_approval" and spy.ran() == []


async def test_the_reviewer_is_not_asked_once_the_run_has_spent_its_budget(make_app):
    """It is a model call like any other. With nothing left to spend the owner decides, which is
    where the reviewer sends everything it is unsure about anyway."""
    model = ScriptedModel(
        [
            ModelStep(output=GIT_PUSH, usage=usage(1000, 100)),  # costs 0.003: the whole allowance
            verdict("approve", "must never be asked"),
        ]
    )
    app = make_app(
        model,
        config=Config(policy=PolicyConfig(mode="auto"), budget=BudgetConfig(per_run_usd=0.003)),
    )
    spy = install(app, Spy())
    [result] = await ask(app, "please push")

    assert result.outcome.status == "awaiting_approval" and spy.ran() == []
    assert len(model.calls) == 1  # the reviewer was not called
    [row] = app.audit.recent()
    assert (row.verdict, row.layer) == ("ask", "guardian") and "budget" in row.reason


async def test_auto_mode_does_not_consult_the_reviewer_for_harmless_calls(make_app):
    view = [function_call("memory", {"command": "view", "path": "/memories"}, call_id="m1")]
    model = ScriptedModel([view, [assistant_message("ok")]])
    app = make_app(model, config=config(mode="auto"))
    [result] = await ask(app)
    assert result.outcome.status == "completed" and len(model.calls) == 2


# ---- tool hygiene ---------------------------------------------------------------------------
async def test_tool_output_is_scrubbed_of_secrets_and_cut_to_size(make_app):
    model = ScriptedModel([[function_call("leaky", {}, call_id="l1")], [assistant_message("ok")]])
    app = make_app(model)
    install(app, Spy())
    await ask(app)

    out = outputs(app)["l1"]
    assert "sk-aaaa" not in out and "[REDACTED" in out
    assert len(out) < 21_000 and "more characters omitted" in out


async def test_a_slow_tool_is_abandoned_and_the_model_is_told(make_app):
    model = ScriptedModel([GIT_STATUS, [assistant_message("It timed out.")]])
    app = make_app(
        model,
        config=Config(
            policy=PolicyConfig(allow=["shell"]), runtime=RuntimeConfig(tool_timeout_s=0.05)
        ),
    )
    install(app, Spy(), delay=5)
    [result] = await ask(app)

    assert result.outcome.status == "completed"
    assert "did not finish within" in outputs(app)["c1"]
    assert app.audit.recent()[0].outcome == "timeout"


async def test_audit_arguments_never_contain_secrets(make_app):
    leak = [function_call("notes", {"text": "token sk-" + "b" * 30}, call_id="n1")]
    model = ScriptedModel([leak])
    app = make_app(model)
    install(app, Spy())
    await ask(app)
    [row] = app.audit.recent()
    assert "sk-bbb" not in row.args and "REDACTED" in row.args
    assert "sk-bbb" not in app.approvals.pending()[0].summary
