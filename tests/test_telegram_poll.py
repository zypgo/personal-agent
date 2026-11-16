from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import telegram_fake as tg
from personal_agent.channels.base import ChannelError
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.channels.telegram.channel import (
    CONFLICT_WAIT_S,
    HANDLE_ATTEMPTS,
    OFFSET_KEY,
    UNSUPPORTED,
    TelegramChannel,
)
from personal_agent.config import Config, TelegramConfig
from personal_agent.core.approvals import PendingCall
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.sessions import DEFAULT_SESSION
from telegram_fake import TOKEN, Fail, FakeDaemon, FakeTelegram
from test_ingress import FakeTranscriber, events

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


class Sleeps:
    def __init__(self):
        self.waited: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


@dataclass
class Rig:
    app: Any
    channel: TelegramChannel
    fake: FakeTelegram
    sleeps: Sleeps
    transcriber: FakeTranscriber

    async def poll(self, *batches: list[dict[str, Any]], more: Any = None) -> FakeDaemon:
        """Run the channel against `batches` of updates until they have all been fetched."""
        for batch in batches:
            self.fake.deliver(*batch)
        daemon = FakeDaemon(stop_when=more or (lambda: not self.fake.batches))
        await self.channel.run(daemon)
        return daemon

    def replies(self) -> list[str]:
        return self.fake.texts()

    def offset(self) -> str | None:
        return self.app.db.kv_get(OFFSET_KEY)


@pytest.fixture
def rig(make_app) -> Rig:
    fake = FakeTelegram()
    transcriber = FakeTranscriber()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)), transcriber=transcriber)
    sleeps = Sleeps()
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()), sleep=sleeps)
    return Rig(app, channel, fake, sleeps, transcriber)


@pytest.fixture
def paired(rig) -> Rig:
    rig.app.pairing.allow("telegram", "7", "me")
    return rig


# ---- strangers ---------------------------------------------------------------------------------
async def test_a_stranger_is_told_how_to_pair_once_and_costs_the_agent_nothing(rig):
    await rig.poll(
        [tg.text(1, "hello?", sender=99, first_name="Eve")],
        [tg.text(2, "hello again", sender=99, first_name="Eve")],
    )
    [reply] = rig.replies()
    [request] = rig.app.pairing.pending("telegram")
    assert f"pa channels approve telegram {request.code}" in reply
    assert request.notified_at is not None and request.label == "Eve"
    assert events(rig.app) == [] and rig.app.transcript.latest() == []
    assert rig.app.inbox.counts()["pending"] == 0
    assert rig.offset() == "3"


async def test_a_code_that_could_not_be_delivered_is_sent_with_the_next_message(rig):
    rig.fake.script("sendMessage", Fail(500, "oops"))
    await rig.poll([tg.text(1, "hi", sender=99)])
    assert rig.app.pairing.pending("telegram")[0].notified_at is None
    await rig.poll([tg.text(2, "hi again", sender=99)])
    assert rig.app.pairing.pending("telegram")[0].notified_at is not None
    assert len(rig.replies()) == 2


async def test_only_a_few_strangers_are_answered_at_a_time(rig):
    await rig.poll([tg.text(n, "hi", sender=100 + n) for n in range(1, 6)])
    assert len(rig.replies()) == 3
    assert len(rig.app.pairing.pending("telegram")) == 3


async def test_once_approved_the_same_person_gets_through(rig):
    await rig.poll([tg.text(1, "let me in", sender=99)])
    code = rig.app.pairing.pending("telegram")[0].code
    rig.app.pairing.approve(code)
    await rig.poll([tg.text(2, "now?", sender=99)])
    [event] = events(rig.app)
    assert event["payload"]["text"] == "now?" and event["payload"]["recipient"] == "99"


async def test_a_name_with_control_characters_cannot_scramble_the_terminal(rig):
    await rig.poll([tg.text(1, "hi", sender=99, first_name="Eve\x1b[2J\u202e")])
    label = rig.app.pairing.pending("telegram")[0].label
    assert "\x1b" not in label and "\u202e" not in label


# ---- the owner --------------------------------------------------------------------------------
async def test_the_owners_text_becomes_an_event_and_wakes_the_worker(paired):
    daemon = await paired.poll([tg.text(5, "what's next?")])
    [event] = events(paired.app)
    assert (event["source"], event["external_id"]) == ("telegram", "tg:5")
    assert event["payload"] == {"text": "what's next?", "recipient": "7"}
    assert daemon.woken == 1
    assert [t.text for t in paired.app.transcript.latest()] == ["what's next?"]
    assert paired.replies() == []  # the answer comes later, through the outbox
    assert paired.offset() == "6"


async def test_an_update_delivered_twice_is_handled_once(paired):
    await paired.poll([tg.text(5, "once")], [tg.text(5, "once")])
    assert len(events(paired.app)) == 1


async def test_the_last_person_heard_from_is_where_background_messages_go(paired):
    paired.app.pairing.allow("telegram", "8")
    paired.app.clock.advance(60)
    await paired.poll([tg.text(1, "hi", sender=8)])
    assert paired.app.pairing.recent_sender("telegram").sender == "8"


@pytest.mark.parametrize(
    "update",
    [
        tg.text(1, "in a group", chat_type="supergroup", chat_id=-100),
        tg.text(1, "a bot", is_bot=True),
        {
            "update_id": 1,
            "message": {"message_id": 1, "chat": {"id": 7, "type": "private"}, "text": "no sender"},
        },
        {"update_id": 1, "edited_message": {"text": "edited"}},
        {"update_id": 1, "channel_post": {"text": "post"}},
        {"update_id": 1, "message_reaction": {}},
    ],
)
async def test_conversations_that_are_not_private_chats_with_a_person_are_ignored(paired, update):
    await paired.poll([update])
    assert events(paired.app) == [] and paired.fake.sent() == []
    assert paired.offset() == "2"


# ---- pictures, voice, other files -----------------------------------------------------------
async def test_a_photo_is_downloaded_in_a_sensible_size_and_given_to_the_agent(paired):
    for name in ("s", "m", "x", "huge"):
        paired.fake.file(name, PNG + name.encode())
    sizes = [
        {"file_id": "s", "width": 90, "height": 90},
        {"file_id": "m", "width": 320, "height": 320},
        {"file_id": "x", "width": 1280, "height": 1280},
        {"file_id": "huge", "width": 4000, "height": 4000},
    ]
    await paired.poll([tg.message(1, photo=sizes, caption="what is this?")])
    assert paired.fake.params("getFile") == [{"file_id": "x"}]
    [event] = events(paired.app)
    assert event["payload"]["text"] == "what is this?"
    [attachment] = event["payload"]["attachments"]
    assert paired.app.media.read(attachment["id"])[0] == PNG + b"x"


async def test_when_every_size_is_huge_the_smallest_is_used(paired):
    paired.fake.file("a", PNG)
    paired.fake.file("b", PNG + b"b")
    sizes = [
        {"file_id": "a", "width": 2000, "height": 2000},
        {"file_id": "b", "width": 5000, "height": 5000},
    ]
    await paired.poll([tg.message(1, photo=sizes)])
    assert paired.fake.params("getFile") == [{"file_id": "a"}]


async def test_a_picture_sent_as_a_file_is_read_like_a_photo(paired):
    paired.fake.file("doc", PNG)
    await paired.poll([tg.message(1, document={"file_id": "doc", "mime_type": "image/png"})])
    assert events(paired.app)[0]["payload"]["attachments"][0]["mime"] == "image/png"


async def test_a_voice_message_is_transcribed_and_marked_as_spoken(paired):
    paired.fake.file("v", b"\x00" * 3000)
    paired.transcriber.words = "remind me at five"
    await paired.poll(
        [tg.message(1, voice={"file_id": "v", "mime_type": "audio/ogg", "duration": 7})]
    )
    [event] = events(paired.app)
    assert event["payload"]["text"] == "remind me at five" and event["payload"]["voice"] is True
    assert paired.transcriber.calls == [(3000, "audio/ogg", "")]


async def test_an_audio_file_is_treated_as_speech_too(paired):
    paired.fake.file("a", b"\x00" * 100)
    await paired.poll(
        [tg.message(1, audio={"file_id": "a", "mime_type": "audio/mpeg", "file_name": "memo.mp3"})]
    )
    assert paired.transcriber.calls == [(100, "audio/mpeg", "memo.mp3")]


@pytest.mark.parametrize(
    "content",
    [
        {"document": {"file_id": "d", "mime_type": "application/pdf"}},
        {"document": {"file_id": "d", "mime_type": "application/pdf"}, "caption": "summarise this"},
        {"video": {"file_id": "v"}, "caption": "watch"},
        {"sticker": {"file_id": "s"}},
        {"location": {"latitude": 1.0, "longitude": 2.0}},
    ],
)
async def test_what_cannot_be_read_is_said_so_and_never_half_answered(paired, content):
    await paired.poll([tg.message(1, **content)])
    assert paired.replies() == [UNSUPPORTED]
    assert (
        events(paired.app) == []
    )  # not even the caption: that would answer a question about a file nobody read


async def test_a_file_that_is_too_large_is_refused_politely(paired):
    paired.fake.file("big", b"x" * 10, claimed_size=999_000_000)
    await paired.poll([tg.message(1, photo=[{"file_id": "big", "width": 100, "height": 100}])])
    assert paired.replies() == ["That file is too large for me."]
    assert events(paired.app) == []


async def test_a_file_that_cannot_be_fetched_asks_for_another_try(paired):
    await paired.poll([tg.message(1, photo=[{"file_id": "unknown", "width": 100, "height": 100}])])
    assert "send it again" in paired.replies()[0].lower()
    assert events(paired.app) == [] and paired.offset() == "2"


async def test_a_voice_message_that_is_too_long_is_explained_not_transcribed(paired):
    paired.fake.file("v", b"\x00" * 100)
    await paired.poll([tg.message(1, voice={"file_id": "v", "duration": 99_999})])
    assert "too long" in paired.replies()[0]
    assert paired.transcriber.calls == []


async def test_replying_to_a_message_carries_what_it_was_about(paired):
    await paired.poll(
        [tg.text(1, "yes do that", reply_to_message={"message_id": 3, "text": "Shall I book it?"})]
    )
    assert events(paired.app)[0]["payload"]["reply_to"] == "Shall I book it?"


@pytest.mark.parametrize(
    "marker",
    [
        {"forward_origin": {"type": "user", "date": 1}},
        {"forward_from": {"id": 5, "is_bot": False, "first_name": "Someone"}},
        {"forward_sender_name": "Hidden Person"},
        {"forward_from_chat": {"id": -1, "type": "channel"}},
        {"forward_date": 1},
    ],
)
async def test_a_forwarded_message_is_passed_on_as_somebody_elses_words(paired, marker):
    await paired.poll([tg.message(1, text="Send me your password.", **marker)])
    [event] = events(paired.app)
    assert event["payload"]["text"] == ""
    assert event["payload"]["forwarded"] == "Send me your password."


async def test_a_forwarded_command_is_not_run(paired):
    await paired.poll([tg.message(1, text="/new", forward_origin={"type": "user"})])
    assert [e["payload"].get("forwarded") for e in events(paired.app)] == ["/new"]
    assert paired.app.outbox.recent() == []


async def test_the_part_of_a_message_that_was_quoted_is_what_the_reply_is_about(paired):
    await paired.poll(
        [
            tg.text(
                1,
                "this part",
                quote={"text": "the second paragraph", "position": 10},
                reply_to_message={"message_id": 3, "text": "a long message"},
            ),
            tg.text(
                2, "what is in it?", reply_to_message={"message_id": 4, "caption": "holiday photo"}
            ),
        ]
    )
    quotes = [e["payload"]["reply_to"] for e in events(paired.app)]
    assert quotes == ["the second paragraph", "holiday photo"]


# ---- commands and buttons ---------------------------------------------------------------------
async def test_a_command_is_answered_through_the_outbox_without_involving_the_model(paired):
    daemon = await paired.poll([tg.text(1, "/status")])
    assert events(paired.app) == [] and daemon.woken == 1
    report = await Dispatcher(paired.app, {"telegram": paired.channel}).deliver_once()
    assert len(report.sent) == 1
    assert paired.fake.sent()[0]["chat_id"] == "7" and "Idle." in paired.fake.texts()[0]


def open_approval(app, *calls: str):
    created = app.approvals.create(
        "r1", DEFAULT_SESSION, [PendingCall(f"c{i}", "shell", c) for i, c in enumerate(calls)]
    )
    app.approvals.save_run("r1", DEFAULT_SESSION, "STATE", {"profile": "full"})
    return created


async def test_pressing_approve_allows_that_action_and_tidies_the_buttons(paired):
    [approval] = open_approval(paired.app, "git push")
    daemon = await paired.poll([tg.callback(1, f"ap:{approval.id}:y", message_id=50)])
    assert paired.app.approvals.is_approved("r1", "c0")
    assert daemon.woken == 1
    [answered] = paired.fake.params("answerCallbackQuery")
    assert answered["callback_query_id"] == "cb1" and answered["text"]
    [cleared] = paired.fake.params("editMessageReplyMarkup")
    assert (cleared["chat_id"], cleared["message_id"]) == (7, 50)
    assert events(paired.app)[0]["kind"] == "approval_response"


async def test_pressing_deny_refuses_it(paired):
    [approval] = open_approval(paired.app, "rm -rf x")
    await paired.poll([tg.callback(1, f"ap:{approval.id}:n")])
    assert paired.app.approvals.get(approval.id).status == "denied"
    assert paired.app.approvals.get(approval.id).decided_by == "owner:telegram"


async def test_deny_all_refuses_everything_waiting(paired):
    open_approval(paired.app, "a", "b")
    await paired.poll([tg.callback(1, "ap:all:n")])
    assert paired.app.approvals.pending() == []


async def test_a_stranger_cannot_press_the_owners_buttons(paired):
    [approval] = open_approval(paired.app, "git push")
    await paired.poll([tg.callback(1, f"ap:{approval.id}:y", sender=99)])
    assert len(paired.app.approvals.pending()) == 1
    assert paired.fake.count("answerCallbackQuery") == 1  # the spinner stops, nothing else happens
    assert paired.fake.count("editMessageReplyMarkup") == 0


@pytest.mark.parametrize(
    "data", ["", "ap", "ap:1", "ap:1:maybe", "ap:x:y", "xx:1:y", "ap:1:y:z", "ap:-1:y"]
)
async def test_a_button_with_odd_data_is_ignored(paired, data):
    [approval] = open_approval(paired.app, "git push")
    await paired.poll([tg.callback(1, data)])
    assert len(paired.app.approvals.pending()) == 1 and approval.id
    assert paired.fake.count("answerCallbackQuery") == 1


async def test_pressing_a_button_for_an_action_already_decided_says_so(paired):
    [approval] = open_approval(paired.app, "git push")
    paired.app.approvals.decide(approval.id, True, by="cli")
    await paired.poll([tg.callback(1, f"ap:{approval.id}:y")])
    assert "already" in paired.fake.params("answerCallbackQuery")[0]["text"]


# ---- where it left off ------------------------------------------------------------------------
async def test_polling_resumes_after_the_last_update_that_was_handled(paired):
    await paired.poll([tg.text(10, "a"), tg.text(11, "b")])
    assert paired.offset() == "12"
    await paired.poll([])
    assert [p["offset"] for p in paired.fake.params("getUpdates")] == [0, 12]


async def test_a_fresh_start_asks_for_everything_telegram_still_holds(paired):
    await paired.poll([])
    assert paired.fake.params("getUpdates")[0]["offset"] == 0


async def test_a_message_that_could_not_be_handled_is_tried_again_before_giving_up(
    paired, monkeypatch
):
    real = Ingress.owner_message
    calls = []

    async def flaky(self, **kw):
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("database is locked")
        return await real(self, **kw)

    monkeypatch.setattr(Ingress, "owner_message", flaky)
    await paired.poll([tg.text(1, "important")])
    assert len(calls) == 2 and paired.sleeps.waited == [1.0]
    assert [e["payload"]["text"] for e in events(paired.app)] == ["important"]


async def test_one_poisonous_update_cannot_block_the_ones_after_it(paired, monkeypatch):
    real = Ingress.owner_message

    async def broken_for_one(self, **kw):
        if kw["text"] == "poison":
            raise RuntimeError("cannot handle this")
        return await real(self, **kw)

    monkeypatch.setattr(Ingress, "owner_message", broken_for_one)
    await paired.poll([tg.text(1, "poison"), tg.text(2, "fine")])
    assert [e["payload"]["text"] for e in events(paired.app)] == ["fine"]
    assert paired.offset() == "3"
    assert paired.sleeps.waited == [float(n) for n in range(1, HANDLE_ATTEMPTS)]
    assert "Something went wrong" in paired.replies()[0]


async def test_an_update_being_handled_when_the_process_stops_is_seen_again_next_time(
    paired, monkeypatch
):
    async def stopped_midway(self, **kw):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch, pytest.raises(asyncio.CancelledError):
        patch.setattr(Ingress, "owner_message", stopped_midway)
        await paired.poll([tg.text(1, "important")])
    assert paired.offset() is None and events(paired.app) == []  # not marked as handled

    await paired.poll([tg.text(1, "important")])  # Telegram hands over what was never confirmed
    assert [e["payload"]["text"] for e in events(paired.app)] == ["important"]
    assert paired.offset() == "2"


# ---- when Telegram is not cooperating ---------------------------------------------------------------
async def test_a_network_outage_is_waited_out_with_growing_pauses_and_then_forgotten(paired):
    paired.fake.script("getUpdates", *[httpx.ConnectError("down")] * 3)
    paired.fake.deliver(tg.text(1, "after the outage"))
    await paired.poll(more=lambda: paired.fake.count("getUpdates") >= 4)
    assert paired.sleeps.waited == [1.0, 2.0, 4.0]
    assert len(events(paired.app)) == 1


async def test_a_timeout_while_polling_is_just_another_try(paired):
    paired.fake.script("getUpdates", httpx.ReadTimeout("slow"))
    await paired.poll([], more=lambda: paired.fake.count("getUpdates") >= 2)
    assert paired.sleeps.waited == [1.0]


async def test_a_rate_limit_while_polling_is_waited_out_as_told(paired):
    paired.fake.script("getUpdates", Fail(429, "Too Many Requests", retry_after=9))
    await paired.poll([], more=lambda: paired.fake.count("getUpdates") >= 2)
    assert paired.sleeps.waited == [9.0]


async def test_a_webhook_left_over_from_another_setup_is_removed(paired):
    paired.fake.script(
        "getUpdates", Fail(409, "Conflict: can't use getUpdates method while webhook is active")
    )
    await paired.poll([], more=lambda: paired.fake.count("getUpdates") >= 2)
    assert paired.fake.count("deleteWebhook") == 1 and paired.sleeps.waited == []


async def test_another_program_polling_the_same_bot_is_waited_for(paired):
    paired.fake.script("getUpdates", Fail(409, "Conflict: terminated by other getUpdates request"))
    await paired.poll([], more=lambda: paired.fake.count("getUpdates") >= 2)
    assert paired.fake.count("deleteWebhook") == 0
    assert paired.sleeps.waited == [CONFLICT_WAIT_S]


@pytest.mark.parametrize("method", ["getMe", "getUpdates"])
async def test_a_rejected_token_switches_the_channel_off_instead_of_hammering_telegram(
    paired, method
):
    paired.fake.script(method, Fail(401, "Unauthorized"))
    await paired.poll(more=lambda: False)  # would run forever if the channel kept going
    assert paired.fake.count("getUpdates") == (0 if method == "getMe" else 1)


async def test_the_channel_is_off_without_a_token(make_app, monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True)))
    channel = TelegramChannel(app)
    assert not channel.configured
    await asyncio.wait_for(channel.run(FakeDaemon(stop_when=lambda: False)), 2)  # returns at once
    with pytest.raises(ChannelError) as caught:
        channel.api  # noqa: B018
    assert caught.value.permanent and "TELEGRAM_BOT_TOKEN" in str(caught.value)


# ---- "typing..." -------------------------------------------------------------------------------
async def eventually(check, within=3.0):
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


@contextlib.asynccontextmanager
async def typing_loop(rig):
    task = asyncio.create_task(rig.channel._typing_loop(FakeDaemon()))
    await asyncio.sleep(0.02)  # let it subscribe
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_typing_is_shown_while_the_agent_works_on_something_asked_here(paired):
    async with typing_loop(paired):
        with paired.app.live.working(DEFAULT_SESSION, channel="telegram"):
            await eventually(lambda: paired.fake.count("sendChatAction") >= 1)
    assert paired.fake.params("sendChatAction")[0] == {"chat_id": "7", "action": "typing"}


async def test_typing_is_not_shown_for_work_asked_somewhere_else_or_by_the_clock(paired):
    async with typing_loop(paired):
        with paired.app.live.working("main", channel="web"):
            await asyncio.sleep(0.15)
        with paired.app.live.working("timer:x"):
            await asyncio.sleep(0.15)
    assert paired.fake.count("sendChatAction") == 0


async def test_typing_can_be_switched_off(make_app):
    fake = FakeTelegram()
    app = make_app(config=Config(telegram=TelegramConfig(enabled=True, typing=False)))
    channel = TelegramChannel(app, api=TelegramApi(TOKEN, client=fake.client()))
    await asyncio.wait_for(channel._typing_loop(FakeDaemon()), 2)  # returns at once
