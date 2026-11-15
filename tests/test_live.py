from __future__ import annotations

import asyncio
import threading

from personal_agent.core.live import LiveBus


async def test_a_listener_hears_what_is_published_after_it_subscribed():
    bus = LiveBus()
    bus.publish({"type": "before"})
    with bus.subscribe() as events:
        bus.publish({"type": "turn", "id": 1})
        assert await events.next(1) == {"type": "turn", "id": 1}
        assert await events.next(0.05) is None  # nothing else: no replay of old events


async def test_every_listener_gets_its_own_copy_and_a_closed_one_stops_hearing():
    bus = LiveBus()
    first, second = bus.subscribe(), bus.subscribe()
    bus.publish({"n": 1})
    assert await first.next(1) == {"n": 1}
    assert await second.next(1) == {"n": 1}
    first.close()
    bus.publish({"n": 2})
    assert await second.next(1) == {"n": 2}
    assert await first.next(0.05) is None
    assert bus.listeners == 1
    second.close()
    assert bus.listeners == 0


async def test_a_listener_that_stops_reading_loses_the_oldest_events_not_memory():
    bus = LiveBus()
    with bus.subscribe(maxsize=3) as events:
        for n in range(10):
            bus.publish({"n": n})
        await asyncio.sleep(0)  # let the deliveries run
        assert [e["n"] for e in events.drain()] == [7, 8, 9]


async def test_publishing_from_another_thread_reaches_the_listener():
    bus = LiveBus()
    with bus.subscribe() as events:
        worker = threading.Thread(target=lambda: bus.publish({"from": "thread"}))
        worker.start()
        worker.join()
        assert await events.next(1) == {"from": "thread"}


def test_publishing_into_a_loop_that_has_ended_neither_raises_nor_lingers():
    bus = LiveBus()

    async def listen():
        bus.subscribe()

    asyncio.run(listen())  # the subscriber's loop is now closed
    assert bus.listeners == 1
    bus.publish({"type": "turn"})  # must not raise
    assert bus.listeners == 0


async def test_working_is_reported_when_work_starts_and_when_the_last_of_it_ends():
    bus = LiveBus()
    with bus.subscribe() as events:
        assert not bus.busy
        with bus.working("a", channel="telegram"):
            with bus.working("b"):
                assert bus.busy
            assert bus.busy  # "a" is still going
        assert not bus.busy
        await asyncio.sleep(0)
        seen = events.drain()
    assert [e["working"] for e in seen] == [True, True, True, False]
    assert seen[0]["channel"] == "telegram"
    assert all(e["type"] == "activity" for e in seen)


async def test_a_failing_run_still_ends_the_working_state():
    bus = LiveBus()
    try:
        with bus.working("a"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not bus.busy


async def test_tool_names_are_published_without_arguments():
    bus = LiveBus()
    with bus.subscribe() as events:
        bus.tool_started("web_fetch")
        event = await events.next(1)
    assert event == {"type": "activity", "working": True, "tool": "web_fetch"}


# ---- what a real run publishes ---------------------------------------------------------------------
async def test_a_run_says_when_it_starts_which_tools_it_uses_and_when_it_ends(make_app):
    from agents.testing import ScriptedModel, assistant_message, function_call

    from helpers import owner_message
    from personal_agent.core.worker import Worker

    model = ScriptedModel(
        [
            [function_call("memory", {"command": "view", "path": "/memories"}, call_id="c1")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model)
    with app.live.subscribe() as events:
        app.inbox.add(owner_message("hi", source="telegram"))
        await Worker(app).run_once()
        await asyncio.sleep(0)
        seen = events.drain()
    activity = [e for e in seen if e["type"] == "activity"]
    first = activity[0]
    assert first["working"] is True and first["channel"] == "telegram" and first["tool"] is None
    assert [e["tool"] for e in activity if e["tool"]] == ["memory"]  # the name only, no arguments
    assert activity[-1]["working"] is False
    assert not app.live.busy


async def test_work_started_by_the_clock_is_not_attributed_to_a_chat(make_app):
    from agents.testing import ScriptedModel, assistant_message

    from helpers import timer
    from personal_agent.core.worker import Worker

    app = make_app(ScriptedModel([[assistant_message("nothing to report")]]))
    with app.live.subscribe() as events:
        app.inbox.add(timer("check", "Look around."))
        await Worker(app).run_once()
        await asyncio.sleep(0)
        seen = events.drain()
    assert seen[0]["working"] is True and seen[0].get("channel") is None
