from __future__ import annotations

import asyncio

from personal_agent.core.live import LiveBus
from personal_agent.core.outbox import Outbox
from personal_agent.core.transcript import Transcript


def delivered(db, clock, text="hi", **kw):
    """An outbox message, as a channel would receive it."""
    outbox = Outbox(db, clock)
    kw.setdefault("channel", "telegram")
    message_id = outbox.enqueue(session_key="main", text=text, **kw)
    return outbox.get(message_id)


def test_what_the_owner_says_is_recorded_with_its_channel_and_attachments(db, clock):
    transcript = Transcript(db, clock)
    turn = transcript.add_in(
        "web", "look at this", attachments=[{"kind": "image", "id": "ab"}], meta={"voice": True}
    )
    assert (turn.direction, turn.channel, turn.kind, turn.text) == (
        "in",
        "web",
        "message",
        "look at this",
    )
    assert turn.meta == {"voice": True, "attachments": [{"kind": "image", "id": "ab"}]}
    assert transcript.latest() == [turn]


def test_a_delivered_message_is_recorded_once_whatever_happens_next(db, clock):
    transcript = Transcript(db, clock)
    message = delivered(db, clock, "Your brief", urgency="urgent")
    first = transcript.add_out(message)
    assert first is not None
    assert (first.direction, first.channel, first.text) == ("out", "telegram", "Your brief")
    assert first.meta["urgency"] == "urgent"
    assert transcript.add_out(message) is None  # a second delivery attempt adds nothing
    assert len(transcript.latest()) == 1


def test_approval_requests_keep_the_ids_the_buttons_need(db, clock):
    transcript = Transcript(db, clock)
    message = delivered(
        db, clock, "approve?", kind="approval_request", extra={"approval_ids": [4, 5]}
    )
    turn = transcript.add_out(message)
    assert turn.kind == "approval_request"
    assert turn.meta["approval_ids"] == [4, 5]


def test_history_is_read_in_order_in_pages(db, clock):
    transcript = Transcript(db, clock)
    ids = [transcript.add_in("cli", f"m{n}").id for n in range(7)]
    assert [t.text for t in transcript.latest(3)] == ["m4", "m5", "m6"]  # newest, oldest first
    assert [t.text for t in transcript.latest(3, before=ids[4])] == ["m1", "m2", "m3"]
    assert [t.text for t in transcript.after(ids[4])] == ["m5", "m6"]
    assert [t.text for t in transcript.after(ids[0], limit=2)] == ["m1", "m2"]
    assert transcript.after(ids[-1]) == []
    assert transcript.last_id() == ids[-1]


def test_an_empty_history_has_id_zero(db, clock):
    assert Transcript(db, clock).last_id() == 0
    assert Transcript(db, clock).latest() == []


def test_the_channel_the_owner_last_spoke_on(db, clock):
    transcript = Transcript(db, clock)
    assert transcript.last_in_channel() is None
    transcript.add_in("telegram", "a")
    transcript.add_out(delivered(db, clock, channel="web"))  # what we said does not count
    assert transcript.last_in_channel() == "telegram"
    transcript.add_in("web", "b")
    assert transcript.last_in_channel() == "web"


def test_the_question_can_be_limited_to_the_channels_that_matter(db, clock):
    transcript = Transcript(db, clock)
    transcript.add_in("telegram", "a")
    transcript.add_in("web", "b")
    transcript.add_in("cli", "c")
    assert transcript.last_in_channel(among={"telegram", "web"}) == "web"
    assert transcript.last_in_channel(among={"telegram"}) == "telegram"
    assert transcript.last_in_channel(among={"telegram", "web"}, exclude={"web"}) == "telegram"
    assert transcript.last_in_channel(among={"sms"}) is None


def test_an_empty_list_of_channels_matches_nothing_rather_than_everything(db, clock):
    transcript = Transcript(db, clock)
    transcript.add_in("telegram", "a")
    assert transcript.last_in_channel(among=set()) is None
    assert transcript.last_in_channel(among=[]) is None


def test_channels_can_be_left_out_when_asking_where_the_owner_last_spoke(db, clock):
    transcript = Transcript(db, clock)
    assert transcript.last_in_channel(exclude={"cli"}) is None
    transcript.add_in("telegram", "a")
    transcript.add_in("cli", "b")
    assert transcript.last_in_channel() == "cli"
    assert transcript.last_in_channel(exclude={"cli"}) == "telegram"
    assert transcript.last_in_channel(exclude={"cli", "telegram"}) is None


async def test_a_new_turn_nudges_whoever_is_watching(db, clock):
    live = LiveBus()
    transcript = Transcript(db, clock, live)
    with live.subscribe() as events:
        turn = transcript.add_in("web", "hello")
        assert await events.next(1) == {"type": "turn", "id": turn.id}
        out = transcript.add_out(delivered(db, clock))
        assert await events.next(1) == {"type": "turn", "id": out.id}


async def test_a_duplicate_delivery_does_not_nudge_again(db, clock):
    live = LiveBus()
    transcript = Transcript(db, clock, live)
    message = delivered(db, clock)
    with live.subscribe() as events:
        transcript.add_out(message)
        transcript.add_out(message)
        await asyncio.sleep(0)
        assert len(events.drain()) == 1


def test_to_json_leaves_out_empty_meta(db, clock):
    turn = Transcript(db, clock).add_in("cli", "x")
    assert "meta" not in turn.to_json()
    with_meta = Transcript(db, clock).add_in("cli", "x", meta={"voice": True})
    assert with_meta.to_json()["meta"] == {"voice": True}


async def test_a_history_that_cannot_be_written_never_stops_delivery(make_app, monkeypatch):
    from personal_agent.core.dispatcher import Dispatcher
    from test_dispatcher import FakeChannel, queue

    app = make_app()
    channel = FakeChannel()
    message_id = queue(app, "important")

    def broken(message):
        raise RuntimeError("disk full")

    monkeypatch.setattr(app.transcript, "add_out", broken)
    report = await Dispatcher(app, {"cli": channel}).deliver_once()
    assert report.sent == [message_id] and channel.sent == ["important"]
    assert app.outbox.get(message_id).status == "sent"
