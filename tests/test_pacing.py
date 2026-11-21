"""A short chat reply goes out as a few messages a beat apart; anything with structure stays whole."""

from __future__ import annotations

import json

import pytest

from channel_identity_helpers import bind_owner
from personal_agent.channels import pacing
from personal_agent.channels.dingtalk.api import DingTalkApi
from personal_agent.channels.dingtalk.channel import DingTalkChannel, recipient_key
from personal_agent.channels.dingtalk.config import DingTalkConfig
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.channels.telegram.channel import PartialDelivery, TelegramChannel
from personal_agent.config import ChatConfig, Config, TelegramConfig
from telegram_fake import TOKEN, Fail, FakeTelegram
from test_dingtalk_api import FakeDingTalk

THREE = "好的，我看了一下。\n\n报价比上次低了 8%。\n\n要我现在回复他们吗？"


def message(app, text, **kw):
    kw.setdefault("channel", "telegram")
    kw.setdefault("recipient", "7")
    kw.setdefault("extra", {"interactive": True})
    return app.outbox.get(app.outbox.enqueue(session_key="main", text=text, **kw))


# ---- what is split, and when ---------------------------------------------------------------
def test_paragraphs_become_messages_up_to_the_limit_and_the_rest_joins_the_last():
    assert pacing.bubbles(THREE, 3) == [
        "好的，我看了一下。",
        "报价比上次低了 8%。",
        "要我现在回复他们吗？",
    ]
    assert pacing.bubbles(THREE, 2) == [
        "好的，我看了一下。",
        "报价比上次低了 8%。\n\n要我现在回复他们吗？",
    ]
    assert pacing.bubbles(THREE, 1) == [THREE]
    assert pacing.bubbles("一\n\n\n \n二\n三", 3) == ["一", "二\n三"]  # a line break is not a break


@pytest.mark.parametrize(
    "text",
    [
        "只有一段，没有空行。",
        "",
        "先说结论。\n\n- 第一条\n- 第二条",
        "步骤：\n\n1. 打开\n2. 关闭",
        "1、先做这个\n\n再做那个",
        "一、先做这个\n\n二、再做那个",
        "（1）先做这个\n\n（2）再做那个",
        "## 标题\n\n正文",
        "看这段：\n\n```py\nprint(1)\n```",
        "对比：\n\n| a | b |\n|---|---|",
        "他说：\n\n> 引用的话",
    ],
)
def test_text_with_structure_or_without_paragraphs_is_never_cut(text):
    assert pacing.bubbles(text, 3) == [text]


def test_the_pause_follows_how_long_the_next_message_would_take_to_type():
    assert pacing.pause("嗯") == pytest.approx(0.535)
    assert pacing.pause("字" * 20) < pacing.pause("字" * 40) < pacing.MAX_PAUSE_S
    assert pacing.pause("字" * 500) == pacing.MAX_PAUSE_S


def test_only_a_plain_short_reply_to_someone_waiting_is_split(make_app):
    app = make_app()
    cfg = ChatConfig()

    def ask(text="好的\n\n来了", **kw):
        return message(app, text, **kw)

    assert pacing.chatty(ask(), cfg)
    assert not pacing.chatty(ask(extra={}), cfg)  # a background message: nobody is waiting
    assert not pacing.chatty(ask(urgency="urgent"), cfg)
    assert not pacing.chatty(ask(kind="notice"), cfg)
    assert not pacing.chatty(ask(kind="approval_request", extra={"interactive": True}), cfg)
    assert not pacing.chatty(ask(extra={"interactive": True, "artifacts": [{"name": "a.md"}]}), cfg)
    assert not pacing.chatty(ask("字" * 800), cfg)
    assert not pacing.chatty(ask(), ChatConfig(bubbles=False))


# ---- Telegram ------------------------------------------------------------------------------
class Sleeps:
    def __init__(self):
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def tg(make_app):
    fake = FakeTelegram()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    sleeps = Sleeps()
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()), sleep=sleeps)
    app.pairing.allow("telegram", "7", "me")
    bind_owner(app, "telegram", "7")
    return app, channel, fake, sleeps


async def test_a_reply_arrives_as_separate_messages_with_typing_in_between(tg):
    app, channel, fake, sleeps = tg
    await channel.send(message(app, THREE))
    assert fake.texts() == ["好的，我看了一下。", "报价比上次低了 8%。", "要我现在回复他们吗？"]
    assert fake.count("sendChatAction") == 2  # before the second and the third, not the first
    assert sleeps.waited == [
        pacing.pause("报价比上次低了 8%。"),
        pacing.pause("要我现在回复他们吗？"),
    ]


async def test_a_background_message_or_a_formatted_reply_goes_out_whole(tg):
    app, channel, fake, sleeps = tg
    await channel.send(message(app, THREE, extra={}))
    await channel.send(message(app, "结论：\n\n- 一\n- 二"))
    assert len(fake.sent()) == 2 and fake.count("sendChatAction") == 0 and sleeps.waited == []


async def test_the_chat_settings_decide(make_app):
    fake = FakeTelegram()
    cfg = Config(telegram=TelegramConfig(enabled=True), chat=ChatConfig(max_bubbles=2))
    app = make_app(config=cfg)
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()), sleep=Sleeps())
    app.pairing.allow("telegram", "7", "me")
    bind_owner(app, "telegram", "7")
    await channel.send(message(app, THREE))
    assert len(fake.sent()) == 2
    off = make_app(
        config=Config(telegram=TelegramConfig(enabled=True), chat=ChatConfig(bubbles=False))
    )
    off.pairing.allow("telegram", "7", "me")
    bind_owner(off, "telegram", "7")
    fake2 = FakeTelegram()
    await TelegramChannel(off, api=TelegramApi(TOKEN, client=fake2.client()), sleep=Sleeps()).send(
        message(off, THREE)
    )
    assert fake2.texts() == [THREE]


async def test_a_failure_between_messages_is_reported_as_partly_delivered(tg):
    app, channel, fake, _ = tg
    fake.script("sendMessage", {"message_id": 1}, Fail(403, "blocked"))
    with pytest.raises(PartialDelivery) as caught:
        await channel.send(message(app, THREE))
    assert "1 of 3" in str(caught.value)


async def test_a_typing_indicator_that_fails_does_not_stop_the_reply(tg):
    app, channel, fake, _ = tg
    fake.script("sendChatAction", Fail(500, "boom"), Fail(500, "boom"))
    await channel.send(message(app, THREE))
    assert len(fake.sent()) == 3


async def test_buttons_stay_on_the_last_message(tg):
    app, channel, fake, _ = tg
    approval = message(
        app, THREE, kind="approval_request", extra={"interactive": True, "approval_ids": [3]}
    )
    await channel.send(approval)
    assert len(fake.sent()) == 1 and "reply_markup" in fake.sent()[0]  # one message, as it was


# ---- DingTalk ------------------------------------------------------------------------------
async def test_in_dingtalk_the_messages_are_a_beat_apart(make_app):
    owner = recipient_key("offline-app", "corp", "owner")
    fake = FakeDingTalk()
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    sleeps = Sleeps()
    async with fake.client() as client:
        channel = DingTalkChannel(
            app, api=DingTalkApi("offline-app", "offline-secret", client=client), sleep=sleeps
        )
        app.pairing.allow("dingtalk", owner, "Owner")
        bind_owner(app, "dingtalk", owner)
        await channel.send(message(app, THREE, channel="dingtalk", recipient=owner))
        texts = [json.loads(r["msgParam"])["text"] for r in fake.sent()]
        assert texts == ["好的，我看了一下。", "报价比上次低了 8%。", "要我现在回复他们吗？"]
        assert sleeps.waited == [pacing.pause(texts[1]), pacing.pause(texts[2])]
        await channel.send(message(app, THREE, channel="dingtalk", recipient=owner, extra={}))
        assert len(fake.sent()) == 4 and len(sleeps.waited) == 2
