"""What the owner hears when events that came from outside were given up on."""

from __future__ import annotations

import pytest
from agents.testing import ScriptedModel

from helpers import webhook
from personal_agent.core.worker import Worker

DAY = 86_400


@pytest.fixture(autouse=True)
def early_in_the_day(clock):
    """Nothing here is meant to cross midnight, which would count as a new day."""
    clock.set(19_700 * DAY + 3600)


async def give_up_on(app, count: int, source: str = "github") -> None:
    """Make `count` more events from a webhook die, then let a worker notice."""
    first = app.inbox.counts()["dead"]
    for i in range(count):
        app.inbox.add(webhook(source, i, session=f"{source}-{first}-{i}"))  # a session each
    for _ in range(20):  # each failed claim is one attempt, and the wait between them is skipped
        if app.inbox.counts()["dead"] >= first + count:
            break
        while (batch := app.inbox.claim("w")) is not None:
            app.inbox.fail(batch, "bug")
        app.clock.advance(400)
    assert app.inbox.counts()["dead"] == first + count
    await Worker(app).run_once()


def told(app) -> list[str]:
    return [m.text for m in app.outbox.recent() if "反复出错" in m.text]


FIRST = "来自「webhook:github」的一条事件（webhook）反复出错，已经放弃处理。"


def total(count: int, source: str = "github") -> str:
    return f"今天来自「webhook:{source}」的 {count} 条事件反复出错，已经放弃处理。"


async def test_a_source_that_keeps_failing_is_reported_at_growing_intervals_with_the_count(
    make_app,
):
    app = make_app(ScriptedModel([]))
    heard = []
    for _ in range(8):
        await give_up_on(app, 1)
        heard.append(len(told(app)))

    assert heard == [1, 2, 2, 3, 3, 3, 3, 4]  # at the 1st, 2nd, 4th and 8th
    assert set(told(app)) == {FIRST, total(2), total(4), total(8)}


async def test_several_at_once_are_counted_too(make_app):
    app = make_app(ScriptedModel([]))
    await give_up_on(app, 5)
    assert set(told(app)) == {FIRST, total(2), total(4)}  # the 5th was not worth another


async def test_each_source_is_counted_on_its_own_and_each_day_afresh(make_app, clock):
    app = make_app(ScriptedModel([]))
    await give_up_on(app, 1, "github")
    await give_up_on(app, 1, "stripe")  # its own first
    assert len(told(app)) == 2
    clock.advance(DAY)
    await give_up_on(app, 1, "github")  # a new day: a first again
    assert told(app).count(FIRST) == 2 and len(told(app)) == 3


async def test_what_is_kept_for_counting_does_not_grow_with_the_days(make_app, clock):
    app = make_app(ScriptedModel([]))
    for _ in range(3):
        await give_up_on(app, 1)
        clock.advance(DAY)
    rows = app.db.query("SELECT key FROM kv WHERE key LIKE 'dropped:%'")
    assert [r["key"] for r in rows] == ["dropped:webhook:github"]
