from __future__ import annotations

import re

import httpx
import pytest

from channel_identity_helpers import bind_owner, revoke_owner
from personal_agent.channels.base import ChannelError
from personal_agent.channels.telegram.api import TelegramApi, TelegramApiError
from personal_agent.channels.telegram.channel import (
    MAX_SEND_ATTEMPTS,
    MAX_WAIT_S,
    PartialDelivery,
    TelegramChannel,
    approval_markup,
)
from personal_agent.config import Config, TelegramConfig
from personal_agent.core.dispatcher import Dispatcher
from telegram_fake import TOKEN, Fail, FakeTelegram


class Sleeps:
    def __init__(self):
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@pytest.fixture
def rig(make_app):
    fake = FakeTelegram()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    sleeps = Sleeps()
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()), sleep=sleeps)
    app.pairing.allow("telegram", "7", "me")
    bind_owner(app, "telegram", "7")
    return app, channel, fake, sleeps


def queue(app, text="hello", **kw):
    kw.setdefault("channel", "telegram")
    kw.setdefault("recipient", "7")
    message_id = app.outbox.enqueue(session_key=kw.pop("session", "main"), text=text, **kw)
    return app.outbox.get(message_id)


# ---- the basics ------------------------------------------------------------------------------
async def test_a_message_goes_to_the_chat_it_answers_as_html(rig):
    app, channel, fake, _ = rig
    await channel.send(queue(app, "Hello **there**"))
    [sent] = fake.sent()
    assert sent["chat_id"] == "7"
    assert (sent["text"], sent["parse_mode"]) == ("Hello <b>there</b>", "HTML")
    assert "disable_notification" not in sent and "reply_markup" not in sent


async def test_low_priority_messages_arrive_without_a_sound(rig):
    app, channel, fake, _ = rig
    await channel.send(queue(app, urgency="low"))
    assert fake.sent()[0]["disable_notification"] is True


async def test_a_background_message_goes_to_whoever_wrote_most_recently(rig):
    app, channel, fake, _ = rig
    app.pairing.allow("telegram", "8", "partner")
    bind_owner(app, "telegram", "8")
    app.clock.advance(5)
    app.pairing.touch("telegram", "8")
    await channel.send(queue(app, recipient=None))
    assert fake.sent()[0]["chat_id"] == "8"


async def test_with_nobody_paired_a_background_message_fails_for_good(make_app):
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    fake = FakeTelegram()
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()))
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app, recipient=None))
    assert caught.value.permanent and "pa channels approve" in str(caught.value)
    assert fake.sent() == []


async def test_nothing_is_ever_sent_to_an_account_that_is_not_approved(rig):
    app, channel, fake, _ = rig
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app, recipient="666"))
    assert caught.value.permanent
    assert fake.sent() == []
    app.pairing.revoke("telegram", "7")
    revoke_owner(app, "telegram", "7")
    with pytest.raises(ChannelError):
        await channel.send(queue(app))  # revoked since the message was queued
    assert fake.sent() == []


# ---- when the markup is refused ----------------------------------------------------------------
async def test_if_telegram_cannot_parse_the_html_the_words_go_out_as_plain_text(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", Fail(400, "Bad Request: can't parse entities: Unexpected end tag"))
    await channel.send(queue(app, "a **b** c"))
    first, second = fake.sent()
    assert first["parse_mode"] == "HTML"
    assert "parse_mode" not in second and second["text"] == "a **b** c"


# ---- when it goes wrong ------------------------------------------------------------------------
async def test_a_rate_limit_is_waited_out_and_the_message_still_arrives(rig):
    app, channel, fake, sleeps = rig
    fake.script("sendMessage", Fail(429, "Too Many Requests", retry_after=3))
    await channel.send(queue(app))
    assert fake.count("sendMessage") == 2
    assert sleeps.waited == [3.5]


async def test_a_rate_limit_that_would_take_too_long_is_handed_back_to_the_dispatcher(rig):
    app, channel, fake, sleeps = rig
    fake.script("sendMessage", Fail(429, "Too Many Requests", retry_after=MAX_WAIT_S + 60))
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app))
    assert caught.value.retry_after == MAX_WAIT_S + 60 and not caught.value.permanent
    assert sleeps.waited == [] and fake.count("sendMessage") == 1


async def test_a_rate_limit_that_never_clears_is_given_up_on_as_not_delivered(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", *[Fail(429, "slow down", retry_after=1)] * MAX_SEND_ATTEMPTS)
    with pytest.raises(ChannelError):
        await channel.send(queue(app))
    assert fake.count("sendMessage") == MAX_SEND_ATTEMPTS


async def test_a_server_error_is_retried_and_then_reported_as_unknown_not_as_failed(rig):
    app, channel, fake, sleeps = rig
    fake.script("sendMessage", *[Fail(502, "Bad Gateway")] * MAX_SEND_ATTEMPTS)
    with pytest.raises(TelegramApiError):  # not ChannelError: it may have been delivered
        await channel.send(queue(app))
    assert fake.count("sendMessage") == MAX_SEND_ATTEMPTS
    assert sleeps.waited == [2.0, 4.0]


async def test_a_server_error_that_clears_is_just_a_delay(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", Fail(500, "oops"))
    await channel.send(queue(app))
    assert fake.count("sendMessage") == 2


@pytest.mark.parametrize(
    "description", ["Forbidden: bot was blocked by the user", "Bad Request: chat not found"]
)
@pytest.mark.parametrize("status", [400, 403])
async def test_a_chat_that_cannot_be_reached_fails_for_good(rig, status, description):
    app, channel, fake, _ = rig
    fake.script("sendMessage", Fail(status, description))
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app))
    assert caught.value.permanent
    assert fake.count("sendMessage") == 1


async def test_no_connection_is_a_definite_non_delivery_that_may_be_retried(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", httpx.ConnectError(f"https://api.telegram.org/bot{TOKEN}"))
    with pytest.raises(ChannelError) as caught:
        await channel.send(queue(app))
    assert not caught.value.permanent and TOKEN not in str(caught.value)


async def test_a_timeout_after_sending_is_left_as_it_is_because_it_may_have_arrived(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", httpx.ReadTimeout("no answer"))
    with pytest.raises(httpx.ReadTimeout):
        await channel.send(queue(app))
    assert fake.count("sendMessage") == 1  # and not tried again by the channel itself


# ---- long messages and buttons -----------------------------------------------------------------
LONG = "\n\n".join(f"Paragraph {n} " + "word " * 150 for n in range(20))


async def test_a_long_message_arrives_in_order_in_several_pieces(rig):
    app, channel, fake, _ = rig
    await channel.send(queue(app, LONG))
    texts = fake.texts()
    assert len(texts) > 2 and all(len(t) <= 4096 for t in texts)
    numbers = [int(n) for t in texts for n in re.findall(r"Paragraph (\d+)", t)]
    assert numbers == list(range(20))


async def test_if_a_later_piece_fails_the_message_is_reported_as_partly_delivered(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", {"message_id": 1}, Fail(403, "blocked"))
    with pytest.raises(PartialDelivery) as caught:
        await channel.send(queue(app, LONG))
    assert "1 of" in str(caught.value)


async def test_if_the_first_piece_fails_nothing_was_delivered(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", Fail(403, "blocked"))
    with pytest.raises(ChannelError):
        await channel.send(queue(app, LONG))
    assert fake.count("sendMessage") == 1


async def test_approval_requests_carry_buttons_on_the_last_piece_only(rig):
    app, channel, fake, _ = rig
    await channel.send(queue(app, LONG, kind="approval_request", extra={"approval_ids": [3]}))
    sent = fake.sent()
    assert all("reply_markup" not in s for s in sent[:-1])
    keyboard = sent[-1]["reply_markup"]["inline_keyboard"]
    assert [b["callback_data"] for row in keyboard for b in row] == [
        "ap3:personal-owner:1:3:y",
        "ap3:personal-owner:1:3:n",
    ]


def test_several_requests_get_buttons_each_and_one_for_all():
    rows = approval_markup([3, 4])["inline_keyboard"]
    data = [[b["callback_data"] for b in row] for row in rows]
    assert data == [["ap:3:y", "ap:3:n"], ["ap:4:y", "ap:4:n"], ["ap:all:y", "ap:all:n"]]


def test_there_is_no_all_button_where_all_would_not_cover_the_requests():
    """`all` is what waits in the owner's own conversation: not a timer's or a webhook's."""
    rows = approval_markup([3, 4], all_button=False)["inline_keyboard"]
    assert [[b["callback_data"] for b in row] for row in rows] == [
        ["ap:3:y", "ap:3:n"],
        ["ap:4:y", "ap:4:n"],
    ]


async def test_a_requests_of_a_timer_run_get_no_all_button(rig):
    app, channel, fake, _ = rig
    message = queue(
        app, "two", kind="approval_request", extra={"approval_ids": [3, 4]}, session="timer:brief"
    )
    await channel.send(message)
    keyboard = fake.sent()[-1]["reply_markup"]["inline_keyboard"]
    assert "ap:all:y" not in [b["callback_data"] for row in keyboard for b in row]


def test_a_single_request_has_no_all_button_and_a_flood_is_capped():
    assert len(approval_markup([9])["inline_keyboard"]) == 1
    assert len(approval_markup(list(range(40)))["inline_keyboard"]) == 7  # 6 pairs and "all"


async def test_a_plain_message_never_gets_buttons(rig):
    app, channel, fake, _ = rig
    await channel.send(queue(app, "hi", extra={"approval_ids": [3]}))  # kind is "message"
    assert "reply_markup" not in fake.sent()[0]


# ---- through the dispatcher ---------------------------------------------------------------------
async def test_a_delivered_message_is_marked_sent_and_enters_the_history(rig):
    app, channel, _, _ = rig
    message = queue(app, "Your brief")
    report = await Dispatcher(app, {"telegram": channel}).deliver_once()
    assert report.sent == [message.id]
    assert app.outbox.get(message.id).status == "sent"
    [turn] = app.transcript.latest()
    assert (turn.direction, turn.channel, turn.text) == ("out", "telegram", "Your brief")


async def test_a_message_to_a_blocked_chat_fails_without_pretending_to_have_arrived(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", Fail(403, "Forbidden: bot was blocked by the user"))
    message = queue(app)
    report = await Dispatcher(app, {"telegram": channel}).deliver_once()
    assert report.failed == [message.id] and report.sent == []
    assert app.outbox.get(message.id).status == "failed"
    assert app.transcript.latest() == []


async def test_a_timeout_makes_the_dispatcher_hold_back_instead_of_sending_twice(rig):
    app, channel, fake, _ = rig
    fake.script("sendMessage", httpx.ReadTimeout("no answer"))
    message = queue(app)
    report = await Dispatcher(app, {"telegram": channel}).deliver_once()
    assert report.unknown == [message.id]
    assert app.outbox.get(message.id).status == "maybe_sent"
    again = await Dispatcher(app, {"telegram": channel}).deliver_once()
    assert again.sent == [] and fake.count("sendMessage") == 1
