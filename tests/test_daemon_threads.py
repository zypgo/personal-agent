"""The running service looks after the tasks handed to other agents without being asked: a result
nobody has looked at is mentioned, a finished task is put away, and a keeper that fails does not
stop the heartbeat."""

from __future__ import annotations

import asyncio
import logging

from agents.testing import ScriptedModel, assistant_message

from personal_agent import daemon as daemon_module
from personal_agent.orchestration import threads as threads_module
from personal_agent.presence import HEARTBEAT_KEY
from test_daemon import cfg, eventually, running
from test_threads import Desk, said


async def test_the_service_reminds_about_a_result_nobody_has_looked_at(
    make_app, clock, monkeypatch
):
    monkeypatch.setattr(daemon_module, "HOUSEKEEPING_S", 0.05)
    monkeypatch.setattr(threads_module, "SLEEP_S", 24 * 3600)  # the jump below is not a sleep
    app = make_app(ScriptedModel([[assistant_message("ok")]]), config=cfg())
    app.threads.follow()  # start from now
    desk = Desk(app)
    desk.approve()
    desk.claim()
    desk.hand_in()
    app.threads.follow()
    heard = len(said(app))

    async with running(app, {}):
        await asyncio.sleep(0.3)  # the service is up and has looked once: nothing is said yet
        assert len(said(app)) == heard
        clock.advance(46 * 60)
        await eventually(lambda: len(said(app)) > heard, what="the reminder")

    assert "还没验收" in said(app)[-1]["text"]


async def test_the_service_puts_a_finished_task_away_after_a_day(make_app, clock, monkeypatch):
    monkeypatch.setattr(daemon_module, "HOUSEKEEPING_S", 0.05)
    app = make_app(ScriptedModel([[assistant_message("ok")]]), config=cfg())
    app.threads.follow()
    desk = Desk(app)
    desk.approve()
    desk.claim()
    desk.hand_in()
    desk.accept()
    app.threads.follow()
    assert desk.task["id"] not in app.threads.archived_ids()
    clock.advance(25 * 3600)

    async with running(app, {}):
        await eventually(
            lambda: desk.task["id"] in app.threads.archived_ids(), what="the task put away"
        )


async def test_a_keeper_that_fails_is_noticed_and_the_service_carries_on(
    make_app, monkeypatch, caplog
):
    monkeypatch.setattr(daemon_module, "HOUSEKEEPING_S", 0.05)
    app = make_app(ScriptedModel([[assistant_message("ok")]]), config=cfg())
    calls: list[int] = []

    def broken() -> int:
        calls.append(1)
        raise RuntimeError("the keeper broke")

    monkeypatch.setattr(app.threads, "tick", broken)
    with caplog.at_level(logging.ERROR):
        async with running(app, {}) as daemon:
            await eventually(lambda: len(calls) >= 3, what="the loop to keep going")
    assert daemon.stats.errors >= 3
    assert "housekeeping failed" in caplog.text
    assert app.db.kv_get(HEARTBEAT_KEY) is not None  # it was still alive when it started


async def test_a_service_without_tasks_stays_quiet_however_much_time_passes(
    make_app, clock, monkeypatch
):
    monkeypatch.setattr(daemon_module, "HOUSEKEEPING_S", 0.05)
    app = make_app(ScriptedModel([[assistant_message("ok")]]), config=cfg())
    ticks: list[int] = []
    real = app.threads.tick

    def counted() -> int:
        ticks.append(1)
        return real()

    monkeypatch.setattr(app.threads, "tick", counted)
    clock.advance(3 * 86_400)
    async with running(app, {}) as daemon:
        await eventually(lambda: len(ticks) >= 3, what="a few rounds of housekeeping")
    assert said(app) == [] and daemon.stats.errors == 0
