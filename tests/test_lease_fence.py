"""A run keeps its events while it is alive, and stops when it no longer has them.

Leases are timed by the wall clock, which jumps when a laptop sleeps. Before this, a run that was
waiting for a slow tool looked abandoned after such a jump: a second worker took the same message
and ran it again, and an action with a side effect (a charge, a mail) happened twice.
"""

from __future__ import annotations

import asyncio

from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message
from personal_agent.config import PolicyConfig
from personal_agent.core import worker as worker_module
from personal_agent.core.worker import Worker
from test_daemon import cfg, eventually, running
from test_dispatcher import FakeChannel
from tooling import Spy, install


async def test_a_run_that_outlives_its_lease_is_not_started_a_second_time(make_app, clock):
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "charge-the-card"}, call_id="a1")],
            [assistant_message("charged once")],
            # what a second run of the same message would have been given
            [function_call("shell", {"command": "charge-the-card"}, call_id="b1")],
            [assistant_message("charged twice")],
        ]
    )
    app = make_app(model, config=cfg(policy=PolicyConfig(allow=["shell"])))
    spy = install(app, Spy(), delay=0.5)
    channel = FakeChannel()
    async with running(app, {"cli": channel}) as daemon:
        daemon.submit(owner_message("charge the card once"))
        await eventually(lambda: spy.ran(), what="the first run's tool to start")
        clock.advance(app.inbox.lease_s + 1)  # the laptop slept past the lease
        await eventually(lambda: channel.sent, within=5, what="the reply")
        await asyncio.sleep(0.3)  # long enough for a second run to show itself, had one started
    assert spy.ran() == ["charge-the-card"]
    assert channel.sent == ["charged once"]
    assert app.inbox.counts()["done"] == 1
    assert [run.status for run in app.runs.recent()] == ["completed"]


async def test_a_run_that_loses_its_lease_is_stopped_and_leaves_the_events_alone(
    make_app, monkeypatch
):
    monkeypatch.setattr(worker_module, "MIN_HEARTBEAT_S", 0.02)
    model = ScriptedModel(
        [[function_call("shell", {"command": "slow"}, call_id="a1")], [assistant_message("never")]]
    )
    app = make_app(model, config=cfg(policy=PolicyConfig(allow=["shell"])))
    app.inbox.lease_s = 0.06  # so the heartbeat comes every 0.02 s
    spy = install(app, Spy(), delay=30)
    event_id = app.inbox.add(owner_message("do the slow thing")).id

    task = asyncio.create_task(Worker(app).run_once())
    await eventually(lambda: spy.ran(), what="the tool to start")
    with app.db.tx():  # somebody else owns the event now
        app.db.execute("UPDATE events SET lease_owner = 'another-worker' WHERE id = ?", (event_id,))
    result = await asyncio.wait_for(task, 3)

    assert result is not None and result.batch.lost
    assert (
        result.outcome is None and result.failure is None
    )  # nothing to report: not ours to settle
    [run] = app.runs.recent()
    assert run.status == "cancelled"
    event = app.inbox.get(event_id)
    assert (event["status"], event["lease_owner"]) == ("leased", "another-worker")
    assert app.outbox.counts()["pending"] == 0  # the owner is not told anything about it
