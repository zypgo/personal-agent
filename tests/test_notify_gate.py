"""Quiet hours and the hourly limit protect the owner's attention from background work, and never
delay an answer the owner is waiting for or an urgent alert."""

from __future__ import annotations

from datetime import UTC, datetime

from personal_agent.config import Config, NotifyConfig
from personal_agent.core.dispatcher import Dispatcher
from test_dispatcher import FakeChannel, queue

HOUR = 3600


def epoch(*args) -> float:
    return datetime(*args, tzinfo=UTC).timestamp()


def app_with(make_app, **notify):
    return make_app(config=Config(notify=NotifyConfig(**notify)))


async def deliver(app, channel):
    return await Dispatcher(app, {"cli": channel}).deliver_once()


async def test_background_messages_wait_out_quiet_hours(make_app, clock):
    app = app_with(make_app, quiet_hours="22:00-08:00")
    clock.set(epoch(2026, 11, 15, 23, 30))
    channel = FakeChannel()
    message_id = queue(app, "your weekly brief")

    report = await deliver(app, channel)

    assert report.held == [message_id] and channel.sent == []
    held = app.outbox.get(message_id)
    assert held.status == "pending" and held.attempts == 0  # waiting is free
    assert held.not_before == epoch(2026, 11, 16, 8, 0)

    clock.set(epoch(2026, 11, 16, 8, 1))
    report = await deliver(app, channel)
    assert report.sent == [message_id] and channel.sent == ["your weekly brief"]


async def test_urgent_alerts_and_live_answers_ignore_quiet_hours(make_app, clock):
    app = app_with(make_app, quiet_hours="22:00-08:00")
    clock.set(epoch(2026, 11, 15, 23, 30))
    channel = FakeChannel()
    queue(app, "your flight was cancelled", urgency="urgent")
    queue(app, "here is the answer you asked for", extra={"interactive": True})
    queue(app, "background chatter")

    report = await deliver(app, channel)

    assert sorted(channel.sent) == ["here is the answer you asked for", "your flight was cancelled"]
    assert len(report.held) == 1


async def test_outside_quiet_hours_everything_flows(make_app, clock):
    app = app_with(make_app, quiet_hours="22:00-08:00")
    clock.set(epoch(2026, 11, 15, 12, 0))
    channel = FakeChannel()
    queue(app, "noon update")
    assert (await deliver(app, channel)).sent and channel.sent == ["noon update"]


async def test_quiet_hours_are_evaluated_in_the_owners_timezone(make_app, clock):
    from personal_agent.config import AgentConfig

    cfg = Config(
        agent=AgentConfig(timezone="Asia/Shanghai"), notify=NotifyConfig(quiet_hours="23:00-08:00")
    )
    app = make_app(config=cfg)
    clock.set(epoch(2026, 11, 15, 16, 0))  # 00:00 next day in Shanghai: quiet
    channel = FakeChannel()
    queue(app, "late")
    report = await deliver(app, channel)
    assert report.held and app.outbox.list()[0].not_before == epoch(2026, 11, 16, 0, 0)  # 08:00 CST


async def test_the_hourly_limit_spreads_background_messages_out(make_app, clock):
    app = app_with(make_app, max_per_hour=2)
    clock.set(epoch(2026, 11, 15, 12, 0))
    channel = FakeChannel()
    ids = [queue(app, f"update {i}") for i in range(4)]

    report = await deliver(app, channel)
    assert report.sent == ids[:2] and report.held == ids[2:]
    assert channel.sent == ["update 0", "update 1"]
    assert app.outbox.get(ids[2]).not_before == epoch(2026, 11, 15, 13, 0)

    clock.set(epoch(2026, 11, 15, 12, 30))
    assert (await deliver(app, channel)).sent == []  # still inside the hour
    clock.set(epoch(2026, 11, 15, 13, 1))
    assert (await deliver(app, channel)).sent == ids[2:]


async def test_urgent_and_live_messages_neither_count_toward_nor_wait_for_the_limit(
    make_app, clock
):
    app = app_with(make_app, max_per_hour=1)
    clock.set(epoch(2026, 11, 15, 12, 0))
    channel = FakeChannel()
    queue(app, "alert 1", urgency="urgent")
    queue(app, "alert 2", urgency="urgent")
    queue(app, "live answer", extra={"interactive": True})
    queue(app, "background 1")
    queue(app, "background 2")

    report = await deliver(app, channel)

    assert "background 1" in channel.sent and "background 2" not in channel.sent
    assert {"alert 1", "alert 2", "live answer"} <= set(channel.sent)
    assert len(report.held) == 1
