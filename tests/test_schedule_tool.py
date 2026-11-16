"""The `schedule` tool and `send_message(after_sent=...)`, exercised through real runs."""

from __future__ import annotations

from datetime import UTC, datetime

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message, webhook
from personal_agent.channels.base import ChannelError
from personal_agent.config import AgentConfig, Config
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import Trust
from tooling import Spy, drain, install


class FakeChannel:
    name = "cli"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def send(self, message) -> None:
        if self.fail:
            raise ChannelError("offline")
        self.sent.append(message.text)


def full(**args) -> dict:
    """Every parameter, as a strict function schema expects."""
    base = dict(
        action="create", name=None, prompt=None, delay_minutes=None, at=None, cron=None,
        timezone=None, session="own",
    )  # fmt: skip
    return {**base, **args}


def call(args: dict, call_id: str = "s1"):
    return function_call("schedule", args, call_id=call_id)


def last_output(app, key="main") -> str:
    outs = [i["output"] for i in app.sessions.items(key) if i.get("type") == "function_call_output"]
    return outs[-1]


async def ask(app, text="please"):
    app.inbox.add(owner_message(text))
    return await drain(app)


# ---- one-off reminders ----------------------------------------------------------------------
async def test_a_reminder_is_created_without_asking_the_owner(make_app, clock):
    args = full(name="call-mom", prompt="Remind the owner to call mom.", delay_minutes=90)
    model = ScriptedModel([[call(args)], [assistant_message("I will remind you.")]])
    app = make_app(model)
    [result] = await ask(app, "remind me in 90 minutes to call mom")

    assert result.outcome.status == "completed"
    timer = app.timers.get("call-mom")
    assert timer.due_at == clock.now() + 90 * 60 and timer.kind == "followup"
    assert timer.session_key == "timer:call-mom" and timer.created_by == "agent"
    assert timer.trust is Trust.OWNER
    assert last_output(app).startswith("Scheduled.") and "call-mom [one-off]" in last_output(app)
    [row] = app.audit.recent()
    assert (row.category, row.verdict) == ("remind", "allow")


async def test_at_is_read_in_the_owners_timezone(make_app):
    cfg = Config(agent=AgentConfig(timezone="Asia/Shanghai"))
    args = full(name="standup", prompt="Prepare the standup notes.", at="2023-12-02T09:00")
    model = ScriptedModel([[call(args)], [assistant_message("ok")]])
    app = make_app(model, config=cfg)
    await ask(app)
    expected = datetime(2023, 12, 2, 1, 0, tzinfo=UTC).timestamp()  # 09:00 in UTC+8
    assert app.timers.get("standup").due_at == expected


async def test_a_reminder_can_continue_this_conversation(make_app):
    args = full(name="followup", prompt="Check the build.", delay_minutes=5, session="here")
    model = ScriptedModel([[call(args)], [assistant_message("ok")]])
    app = make_app(model)
    await ask(app)
    assert app.timers.get("followup").session_key == "main"


async def test_a_reminder_set_after_reading_outside_content_carries_no_owner_authority(make_app):
    steps = [
        [function_call("fetch", {"url": "https://example.test"}, call_id="f1")],
        [call(full(name="note", prompt="Mention what the page said.", delay_minutes=10))],
        [assistant_message("ok")],
    ]
    app = make_app(ScriptedModel(steps))
    install(app, Spy())
    [result] = await ask(app)

    assert result.outcome.status == "completed"  # a reminder is harmless: no question asked
    assert app.timers.get("note").trust is Trust.UNTRUSTED
    assert "without owner authority" in last_output(app)


# ---- recurring jobs ---------------------------------------------------------------------------
async def test_a_recurring_job_is_a_standing_authorization_and_waits_for_the_owner(make_app):
    args = full(name="brief", prompt="Send the morning brief.", cron="0 8 * * *", timezone="UTC")
    model = ScriptedModel([[call(args)], [assistant_message("Scheduled the brief.")]])
    app = make_app(model)
    [result] = await ask(app, "send me a brief every morning")

    assert result.outcome.status == "awaiting_approval"
    assert app.timers.get("brief") is None  # nothing exists until the owner agrees
    [approval] = app.approvals.pending()
    assert approval.summary == (
        "create brief 0 8 * * * | prompt (23 characters): Send the morning brief."
    )  # the prompt is the whole of what a recurring job may do: the owner reads it

    app.approvals.decide(approval.id, True, by="owner")
    await drain(app)
    timer = app.timers.get("brief")
    assert timer.kind == "schedule" and timer.cron == "0 8 * * *" and timer.tz == "UTC"


async def test_a_recurring_job_that_cannot_work_is_not_put_to_the_owner(make_app):
    steps = [
        [call(full(name="x", prompt="p", cron="every day"))],
        [assistant_message("Let me fix that.")],
    ]
    app = make_app(ScriptedModel(steps))
    [result] = await ask(app)

    assert result.outcome.status == "completed"  # nothing suspended; the model saw the error
    assert app.approvals.pending() == []
    assert "not a valid 5-field cron" in last_output(app) and last_output(app).startswith("Error")
    [row] = app.audit.recent()
    assert (row.layer, row.outcome) == ("invalid", "invalid")


async def test_listing_and_cancelling_need_no_approval(make_app):
    steps = [
        [call(full(name="a", prompt="First.", delay_minutes=60))],
        [call(full(action="list"), "s2")],
        [call(full(action="cancel", name="a"), "s3")],
        [call(full(action="list"), "s4")],
        [assistant_message("done")],
    ]
    app = make_app(ScriptedModel(steps))
    [result] = await ask(app)
    outs = {
        i["call_id"]: i["output"]
        for i in app.sessions.items("main")
        if i.get("type") == "function_call_output"
    }
    assert result.outcome.status == "completed"
    assert "1 pending wake-up(s)" in outs["s2"] and "a [one-off]" in outs["s2"]
    assert outs["s3"] == "Cancelled a."
    assert outs["s4"] == "No pending wake-ups."


async def test_mistakes_come_back_as_errors_the_model_can_fix(make_app):
    cases = [
        full(name="x", prompt="p"),  # no time given
        full(name="x", prompt="p", delay_minutes=5, cron="0 8 * * *"),  # two times given
        full(prompt="p", delay_minutes=5),  # no name
        full(name="x", delay_minutes=5),  # no prompt
        full(name="x", prompt="p", cron="every day"),  # bad cron
        full(name="x", prompt="p", cron="* * * * *"),  # too frequent
        full(name="x", prompt="p", at="tomorrow morning"),  # not ISO
        full(name="x", prompt="p", delay_minutes=-5),  # in the past
        full(action="cancel", name="ghost"),
    ]
    steps = [[call(args, f"c{i}")] for i, args in enumerate(cases)] + [[assistant_message("hm")]]
    app = make_app(ScriptedModel(steps))
    await ask(app)

    outs = [
        i["output"] for i in app.sessions.items("main") if i.get("type") == "function_call_output"
    ]
    assert len(outs) == len(cases)
    assert all(o.startswith("Error") for o in outs), outs
    assert app.timers.scheduled() == []


async def test_a_duplicate_name_is_refused(make_app):
    steps = [
        [call(full(name="dup", prompt="one", delay_minutes=5))],
        [call(full(name="dup", prompt="two", delay_minutes=6), "s2")],
        [assistant_message("ok")],
    ]
    app = make_app(ScriptedModel(steps))
    await ask(app)
    assert last_output(app).startswith("Error") and "already" in last_output(app)
    assert app.timers.get("dup").prompt == "one"


async def test_the_schedule_tool_is_not_offered_to_untrusted_or_dream_runs(make_app):
    model = ScriptedModel([[assistant_message("noted")]])
    app = make_app(model)
    app.inbox.add(webhook("mail", {"body": "hi"}))
    await drain(app)
    assert "schedule" not in {t.name for t in model.calls[0].tools}


# ---- after_sent ---------------------------------------------------------------------------------
def send(text: str, edits: list[dict] | None, call_id: str = "m1"):
    return function_call(
        "send_message",
        {"text": text, "urgency": "normal", "after_sent": edits},
        call_id=call_id,
    )


def edit(**kw) -> dict:
    base = dict(
        command="create", path=None, file_text=None, old_str=None, new_str=None, insert_line=None,
        insert_text=None, old_path=None, new_path=None,
    )  # fmt: skip
    return {**base, **kw}


BOOKMARK = edit(command="create", path="/memories/state/bookmark.md", file_text="reported: 40")


async def test_after_sent_edits_are_applied_only_after_delivery_is_confirmed(make_app):
    model = ScriptedModel([[send("Brief: 3 items", [BOOKMARK])], [assistant_message("sent")]])
    app = make_app(model)
    app.timers.add_followup(name="brief", prompt="Send the brief.", due_at=app.clock.now() + 1)
    app.clock.advance(2)
    app.timers.tick()
    await drain(app)

    assert app.memory.read_text("/memories/state/bookmark.md") is None  # queued, not yet applied
    [message] = [m for m in app.outbox.recent() if m.kind == "message"]
    assert message.on_sent[0]["path"] == "/memories/state/bookmark.md"

    channel = FakeChannel()
    await Dispatcher(app, {"cli": channel}).deliver_once()
    assert channel.sent == ["Brief: 3 items"]
    assert app.memory.read_text("/memories/state/bookmark.md") == "reported: 40"


async def test_a_failed_delivery_never_moves_the_bookmark(make_app):
    model = ScriptedModel([[send("Brief: 3 items", [BOOKMARK])], [assistant_message("sent")]])
    app = make_app(model)
    app.inbox.add(owner_message("brief me"))
    await drain(app)

    await Dispatcher(app, {"cli": FakeChannel(fail=True)}).deliver_once()
    assert app.memory.read_text("/memories/state/bookmark.md") is None


async def test_a_bad_after_sent_edit_is_refused_before_anything_is_queued(make_app):
    bad = [
        edit(
            command="create", path="/memories/preferences/rules.md", file_text="x"
        ),  # not writable
        edit(
            command="create", path="/memories/state/n.md", file_text="key sk-" + "c" * 30
        ),  # secret
        edit(command="create", path="/memories/state/n.md"),  # missing text
        edit(command="create", path="/etc/passwd", file_text="x"),  # outside memory
    ]
    steps = [[send("hello", [e], f"m{i}")] for i, e in enumerate(bad)] + [[assistant_message("hm")]]
    app = make_app(ScriptedModel(steps))
    await ask(app)

    outs = [
        i["output"] for i in app.sessions.items("main") if i.get("type") == "function_call_output"
    ]
    assert len(outs) == len(bad) and all("Nothing was queued" in o for o in outs), outs
    assert [m for m in app.outbox.recent() if m.kind == "message" and m.text == "hello"] == []
