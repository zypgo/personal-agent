"""A whole conversation through the real daemon: Telegram in, the agent, Telegram out."""

from __future__ import annotations

from agents.testing import ScriptedModel, assistant_message, function_call

import telegram_fake as tg
from channel_identity_helpers import bind_owner
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.channels.telegram.channel import TelegramChannel
from personal_agent.config import Config, RuntimeConfig, TelegramConfig
from telegram_fake import TOKEN, FakeTelegram
from test_daemon import eventually, running
from tooling import Spy, install

FAST = RuntimeConfig(poll_s=0.05, concurrency=2)


def build(make_app, model, **kw):
    fake = FakeTelegram()
    app = make_app(model, config=Config(telegram=TelegramConfig(enabled=True), runtime=FAST, **kw))
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()))
    return app, channel, fake


async def test_the_owner_asks_on_telegram_and_the_answer_comes_back_there(make_app):
    app, channel, fake = build(
        make_app, ScriptedModel([[assistant_message("Hello **Frank**, it is sunny.")]])
    )
    app.pairing.allow("telegram", "7", "Frank")
    bind_owner(app, "telegram", "7")
    fake.deliver(tg.text(1, "how is the weather?"))

    async with running(app, {"telegram": channel}, services=[channel]):
        await eventually(lambda: fake.sent(), what="the answer on Telegram")

    [sent] = fake.sent()
    assert (sent["chat_id"], sent["text"], sent["parse_mode"]) == (
        "7",
        "Hello <b>Frank</b>, it is sunny.",
        "HTML",
    )
    assert [(t.direction, t.channel, t.text) for t in app.transcript.latest()] == [
        ("in", "telegram", "how is the weather?"),
        ("out", "telegram", "Hello **Frank**, it is sunny."),
    ]
    assert app.outbox.recent()[0].status == "sent"


async def test_a_new_person_pairs_then_talks_and_nobody_else_ever_costs_a_model_call(make_app):
    model = ScriptedModel([[assistant_message("Nice to meet you.")]])
    app, channel, fake = build(make_app, model)
    fake.deliver(
        tg.text(1, "hello?", sender=99, first_name="Eve"), tg.text(2, "anyone there?", sender=99)
    )

    async with running(app, {"telegram": channel}, services=[channel]):
        await eventually(lambda: fake.sent(), what="the pairing code")
        assert not model.calls  # nothing reached the model
        [request] = app.pairing.pending("telegram")
        assert "/bind" in fake.texts()[0]
        app.pairing.approve(request.code)
        bind_owner(app, "telegram", "99")  # Explicit owner-device link at the trusted computer.
        fake.deliver(tg.text(3, "it is me, Eve", sender=99))
        await eventually(lambda: len(fake.sent()) >= 2, what="the first real answer")

    assert fake.sent()[1]["chat_id"] == "99" and "Nice to meet you." in fake.texts()[1]
    assert len(model.calls) == 1


async def test_an_action_that_needs_approval_is_asked_with_buttons_and_resumes_on_a_tap(make_app):
    push = function_call("shell", {"command": "git push origin main"}, call_id="c1")
    model = ScriptedModel([[push], [assistant_message("Pushed.")]])
    app, channel, fake = build(make_app, model)
    spy = install(app, Spy())
    app.pairing.allow("telegram", "7", "Frank")
    bind_owner(app, "telegram", "7")
    fake.deliver(tg.text(1, "push my work"))

    async with running(app, {"telegram": channel}, services=[channel]):
        await eventually(
            lambda: any("reply_markup" in s for s in fake.sent()), what="the approval request"
        )
        request = next(s for s in fake.sent() if "reply_markup" in s)
        buttons = [b for row in request["reply_markup"]["inline_keyboard"] for b in row]
        assert [b["callback_data"] for b in buttons] == [
            f"ap3:personal-owner:1:{app.approvals.pending()[0].id}:y",
            f"ap3:personal-owner:1:{app.approvals.pending()[0].id}:n",
        ]
        assert spy.ran("shell") == []  # nothing ran yet
        fake.deliver(tg.callback(2, buttons[0]["callback_data"], message_id=77))
        await eventually(
            lambda: "Pushed." in " ".join(fake.texts()), what="the result after the tap"
        )

    assert spy.ran("shell") == ["git push origin main"]
    assert fake.count("answerCallbackQuery") == 1 and fake.count("editMessageReplyMarkup") == 1


async def test_a_denied_action_never_runs(make_app):
    push = function_call("shell", {"command": "git push origin main"}, call_id="c1")
    model = ScriptedModel([[push], [assistant_message("Understood, I did not push.")]])
    app, channel, fake = build(make_app, model)
    spy = install(app, Spy())
    app.pairing.allow("telegram", "7")
    bind_owner(app, "telegram", "7")
    fake.deliver(tg.text(1, "push my work"))

    async with running(app, {"telegram": channel}, services=[channel]):
        await eventually(lambda: app.approvals.pending(), what="the approval request")
        fake.deliver(tg.callback(2, f"ap:{app.approvals.pending()[0].id}:n"))
        await eventually(
            lambda: "did not push" in " ".join(fake.texts()), what="the acknowledgement"
        )

    assert spy.ran() == []


async def test_a_command_gets_an_immediate_answer_without_the_model(make_app):
    model = ScriptedModel([])
    app, channel, fake = build(make_app, model)
    app.pairing.allow("telegram", "7")
    bind_owner(app, "telegram", "7")
    fake.deliver(tg.text(1, "/status"))

    async with running(app, {"telegram": channel}, services=[channel]):
        await eventually(lambda: fake.sent(), what="the status")

    assert "Idle." in fake.texts()[0] and not model.calls


async def test_background_news_reaches_the_phone_the_owner_last_used(make_app):
    from helpers import timer

    model = ScriptedModel(
        [
            [function_call("send_message", {"text": "Your brief is ready."}, call_id="s1")],
            [assistant_message("Told the owner.")],
        ]
    )
    app, channel, fake = build(make_app, model)
    app.pairing.allow("telegram", "7")
    bind_owner(app, "telegram", "7")
    app.transcript.add_in("telegram", "good morning")  # the owner last wrote from Telegram

    async with running(app, {"telegram": channel}, services=[channel]):
        app.inbox.add(timer("brief", "Summarise my inbox."))
        await eventually(lambda: fake.sent(), what="the brief")

    [sent] = fake.sent()
    assert sent["chat_id"] == "7" and sent["text"] == "Your brief is ready."
