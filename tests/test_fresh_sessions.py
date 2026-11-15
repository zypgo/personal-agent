"""A wake-up that stands on its own starts a conversation of its own.

The rule (from Anthropic's "Building always-on agents"): events that continue earlier work resume
the session they belong to, and everything else starts a new one. Here that means the owner
writing, an answer about an approval, or a timer the agent set for "here" resume; a schedule that
fires, a webhook delivery and the nightly dream do not. What such a wake-up needs from before comes
from memory and from what the owner was last told, not from the leftovers of an old conversation.
"""

from __future__ import annotations

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message, text_of, timer, webhook
from personal_agent.core.events import Batch, Event, Kind, NewEvent, Trust
from personal_agent.harness.runner import RESUME_NOTE, Harness
from tooling import Spy, drain, install


def seen(model: ScriptedModel, call: int = -1) -> str:
    """Everything the model was shown in one of its calls, as text."""
    return "\n".join(text_of(item) for item in model.calls[call].input)


def rows(app, key: str) -> list[dict]:
    return app.sessions.transcript(key)


def system_event(name: str, text: str = "") -> NewEvent:
    return NewEvent(
        source="system",
        kind=Kind.SYSTEM,
        session_key=f"system:{name}",
        payload={"name": name, "text": text},
    )


# ---- a schedule fires again ---------------------------------------------------------------
async def test_a_schedule_that_fires_again_starts_a_new_conversation(make_app):
    model = ScriptedModel([[assistant_message("monday done")], [assistant_message("tuesday done")]])
    app = make_app(model)

    app.inbox.add(timer("brief", "MONDAY: make the brief"))
    await drain(app)
    app.inbox.add(timer("brief", "TUESDAY: make the brief"))
    await drain(app)

    second = seen(model)
    assert "TUESDAY: make the brief" in second
    assert "MONDAY" not in second and "monday done" not in second


async def test_what_the_model_sees_in_a_new_conversation_is_only_the_new_wake_up(make_app):
    model = ScriptedModel([[assistant_message("one")], [assistant_message("two")]])
    app = make_app(model)
    app.inbox.add(timer("brief", "first"))
    await drain(app)
    app.inbox.add(timer("brief", "second"))
    await drain(app)

    roles = [i["role"] for i in model.calls[1].input]
    assert roles == ["user"]


async def test_the_log_keeps_what_a_new_conversation_hides(make_app):
    model = ScriptedModel([[assistant_message("monday done")], [assistant_message("tuesday done")]])
    app = make_app(model)
    app.inbox.add(timer("brief", "MONDAY"))
    await drain(app)
    app.inbox.add(timer("brief", "TUESDAY"))
    await drain(app)

    log = rows(app, "timer:brief")
    texts = " ".join(text_of(r["item"]) for r in log if r["kind"] == "item")
    assert "MONDAY" in texts and "monday done" in texts and "TUESDAY" in texts
    assert [r["kind"] for r in log].count("clear") == 1  # the second run only hid the first


async def test_the_first_run_of_a_schedule_has_nothing_to_hide(make_app):
    app = make_app(ScriptedModel([[assistant_message("done")]]))
    app.inbox.add(timer("brief", "go"))
    await drain(app)
    assert [r["kind"] for r in rows(app, "timer:brief")].count("clear") == 0


async def test_two_schedules_do_not_share_a_conversation(make_app):
    model = ScriptedModel([[assistant_message("a")], [assistant_message("b")]])
    app = make_app(model)
    app.inbox.add(timer("alpha", "ALPHA JOB"))
    await drain(app)
    app.inbox.add(timer("beta", "BETA JOB"))
    await drain(app)
    assert "ALPHA JOB" not in seen(model)
    assert app.sessions.items("timer:alpha")  # and the first one was not touched by the second


# ---- webhooks and the nightly dream ----------------------------------------------------------
async def test_a_webhook_delivery_starts_a_new_conversation_and_a_burst_is_one_run(make_app):
    model = ScriptedModel(
        [[assistant_message("handled earlier")], [assistant_message("handled the burst")]]
    )
    app = make_app(model)
    app.inbox.add(webhook("github", {"n": "EARLIER-DELIVERY"}))
    await drain(app)

    for n in (1, 2, 3):
        app.inbox.add(webhook("github", {"n": f"BURST-{n}"}))
    results = await drain(app)

    assert len(results) == 1  # the worker took the three together
    text = seen(model)
    assert all(f"BURST-{n}" in text for n in (1, 2, 3))
    assert "EARLIER-DELIVERY" not in text and "handled earlier" not in text


async def test_the_nightly_dream_starts_a_new_conversation(make_app):
    model = ScriptedModel([[assistant_message("tidied")], [assistant_message("tidied again")]])
    app = make_app(model)
    app.inbox.add(system_event("dream", "DIGEST-ONE"))
    await drain(app)
    app.inbox.add(system_event("dream", "DIGEST-TWO"))
    await drain(app)
    assert "DIGEST-ONE" not in seen(model)


# ---- what does continue --------------------------------------------------------------------
async def test_the_owner_writing_continues_the_conversation(make_app):
    model = ScriptedModel([[assistant_message("hello")], [assistant_message("again")]])
    app = make_app(model)
    app.inbox.add(owner_message("FIRST QUESTION"))
    await drain(app)
    app.inbox.add(owner_message("SECOND QUESTION"))
    await drain(app)
    text = seen(model)
    assert "FIRST QUESTION" in text and "hello" in text and "SECOND QUESTION" in text


async def test_a_timer_set_for_here_resumes_the_conversation_it_was_set_in(make_app):
    model = ScriptedModel([[assistant_message("sure")], [assistant_message("checking")]])
    app = make_app(model)
    app.inbox.add(owner_message("remind me to look at the oven report"))
    await drain(app)

    app.inbox.add(timer("oven", "look again", session="main"))
    await drain(app)

    text = seen(model)
    assert "remind me to look at the oven report" in text and "look again" in text


async def test_a_message_in_a_batch_with_a_timer_keeps_the_conversation(make_app):
    """Same session key, so one batch: the owner's words decide that it is their conversation."""
    model = ScriptedModel([[assistant_message("one")], [assistant_message("two")]])
    app = make_app(model)
    app.inbox.add(owner_message("EARLIER", session="timer:odd"))
    await drain(app)
    app.inbox.add(timer("odd", "tick"))
    app.inbox.add(owner_message("LATER", session="timer:odd"))
    await drain(app)
    assert "EARLIER" in seen(model)


# ---- crashes and waiting --------------------------------------------------------------------
async def test_a_retry_after_a_failed_attempt_resumes_instead_of_starting_over(make_app, clock):
    model = ScriptedModel(
        [
            [assistant_message("monday done")],
            RuntimeError("the network blinked"),
            [assistant_message("tuesday done")],
        ]
    )
    app = make_app(model)
    app.inbox.add(timer("brief", "MONDAY"))
    await drain(app)
    app.inbox.add(timer("brief", "TUESDAY"))
    await drain(app)  # fails: the event waits for a retry
    clock.advance(400)
    await drain(app)

    text = seen(model)
    assert text.count("TUESDAY") == 1  # not written twice
    assert RESUME_NOTE in text
    assert "MONDAY" not in text
    assert [r["kind"] for r in rows(app, "timer:brief")].count("clear") == 1


async def test_a_conversation_waiting_for_approval_is_not_wiped_by_the_next_wake_up(make_app):
    """The run that is waiting still has its question in the log, and the answer must find it."""
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "git push"}, call_id="c1")],
            [assistant_message("second wake-up")],
            [assistant_message("pushed")],
        ]
    )
    app = make_app(model)
    spy = install(app, Spy())

    app.inbox.add(timer("brief", "FIRST FIRING"))
    [first] = await drain(app)
    assert first.outcome.status == "awaiting_approval"

    app.inbox.add(timer("brief", "SECOND FIRING"))
    await drain(app)

    assert [r["kind"] for r in rows(app, "timer:brief")].count("clear") == 0
    assert "FIRST FIRING" in seen(model, 1)  # it carried on in the same conversation

    [approval] = app.approvals.pending()
    app.approvals.decide(approval.id, True, by="owner")
    await drain(app)
    assert spy.ran("shell") == ["git push"]  # the answer found its question, once
    assert app.runs.get(first.outcome.run_id).status == "silent"


async def test_an_outside_event_keeps_its_untrusted_profile_in_a_new_conversation(make_app):
    app = make_app(ScriptedModel([[assistant_message("a")], [assistant_message("b")]]))
    app.inbox.add(webhook("github", {"x": 1}))
    await drain(app)
    app.inbox.add(webhook("github", {"x": 2}))
    await drain(app)
    assert {r.profile for r in app.runs.recent(2)} == {"untrusted"}


# ---- the rule itself ----------------------------------------------------------------------------
def batch_of(key: str, *kinds: str) -> Batch:
    events = [
        Event(
            id=i,
            source="test",
            kind=kind,
            session_key=key,
            payload={},
            external_id=None,
            trust=Trust.OWNER,
            attempts=0,
            created_at=0.0,
        )
        for i, kind in enumerate(kinds, start=1)
    ]
    return Batch(session_key=key, owner="test", events=events, lease_expires_at=0.0)


def test_which_wake_ups_stand_on_their_own():
    new = Harness.starts_new_session
    assert new(batch_of("timer:brief", Kind.TIMER))
    assert new(batch_of("webhook:github", Kind.WEBHOOK))
    assert new(batch_of("system:dream", Kind.SYSTEM))
    assert not new(batch_of("main", Kind.MESSAGE))
    assert not new(batch_of("main", Kind.TIMER))  # a timer set for "here"
    assert not new(batch_of("telegram:42", Kind.TIMER))  # a key that is some conversation's own


def test_what_continues_earlier_work_never_starts_over():
    new = Harness.starts_new_session
    assert not new(batch_of("timer:brief", Kind.APPROVAL_RESPONSE))
    assert not new(batch_of("timer:brief", Kind.TIMER, Kind.APPROVAL_RESPONSE))
    assert not new(batch_of("timer:brief", Kind.TIMER, Kind.MESSAGE))
    assert not new(batch_of("webhook:github", Kind.WEBHOOK, Kind.MESSAGE))
