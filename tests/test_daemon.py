from __future__ import annotations

import asyncio
import contextlib

import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message
from personal_agent import daemon as daemon_module
from personal_agent.config import Config, PolicyConfig, RuntimeConfig
from personal_agent.daemon import Daemon, Engine
from personal_agent.presence import HEARTBEAT_KEY, LOCK_NAME, AlreadyRunning, EngineLock
from test_dispatcher import FakeChannel
from tooling import Spy, install

FAST = RuntimeConfig(poll_s=0.05, concurrency=2)


def cfg(**kw) -> Config:
    return Config(runtime=kw.pop("runtime", FAST), **kw)


async def eventually(check, *, within: float = 3.0, what: str = "condition"):
    deadline = asyncio.get_running_loop().time() + within
    while asyncio.get_running_loop().time() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")


@contextlib.asynccontextmanager
async def running(app, channels=None, **kw):
    daemon = Daemon(app, channels if channels is not None else {"cli": FakeChannel()}, **kw)
    task = asyncio.create_task(daemon.run())
    await asyncio.wait_for(daemon.wait_ready(), 2)
    try:
        yield daemon
    finally:
        daemon.stop()
        await asyncio.wait_for(task, 5)


# ---- the engine lock ----------------------------------------------------------------------
def test_the_lock_is_exclusive_and_released_explicitly(tmp_path):
    path = tmp_path / LOCK_NAME
    first, second = EngineLock(path), EngineLock(path)
    assert first.acquire() and first.held
    assert not second.acquire()
    assert second.holder_pid() is not None
    assert EngineLock.is_taken(path)
    first.release()
    assert not EngineLock.is_taken(path)
    assert second.acquire()
    second.release()


# ---- the loops ----------------------------------------------------------------------------
async def test_the_daemon_answers_owner_messages_and_delivers_the_reply(make_app):
    model = ScriptedModel([[assistant_message("Hello from the daemon.")]])
    app = make_app(model, config=cfg())
    channel = FakeChannel()
    async with running(app, {"cli": channel}) as daemon:
        daemon.submit(owner_message("hi"))
        await eventually(lambda: channel.sent, what="the reply")
    assert channel.sent == ["Hello from the daemon."]
    assert app.inbox.counts()["done"] == 1 and app.outbox.counts()["sent"] == 1
    assert daemon.stats.batches == 1 and daemon.stats.messages_sent == 1


async def test_timers_fire_inside_the_daemon(make_app, clock):
    model = ScriptedModel(
        [
            [function_call("send_message", {"text": "Time to stretch.", "urgency": "normal", "after_sent": None}, call_id="s1")],
            [assistant_message("Told the owner.")],
        ]
    )  # fmt: skip
    app = make_app(model, config=cfg())
    channel = FakeChannel()
    async with running(app, {"cli": channel}) as daemon:
        app.timers.add_followup(name="stretch", prompt="Remind the owner.", due_at=clock.now() + 60)
        await asyncio.sleep(0.15)
        assert channel.sent == []  # not due yet

        clock.advance(61)
        daemon.wake()
        await eventually(lambda: channel.sent, what="the reminder")
    assert channel.sent == ["Time to stretch."]
    assert app.timers.get("stretch").status == "fired"
    assert daemon.stats.timers_fired == 1


async def test_several_sessions_are_all_answered(make_app):
    model = ScriptedModel(
        [[assistant_message("a")], [assistant_message("b")], [assistant_message("c")]]
    )
    app = make_app(model, config=cfg())
    channel = FakeChannel()
    async with running(app, {"cli": channel}) as daemon:
        for session in ("s1", "s2", "s3"):
            daemon.submit(owner_message(f"hello {session}", session=session))
        await eventually(lambda: len(channel.sent) == 3, what="three replies")
    assert sorted(channel.sent) == ["a", "b", "c"]
    assert app.inbox.counts()["done"] == 3


async def test_an_idle_daemon_stays_idle_and_stops_promptly(make_app):
    app = make_app(ScriptedModel([]), config=cfg())
    async with running(app) as daemon:
        await asyncio.sleep(0.2)
        assert daemon.stats.batches == 0 and daemon.stats.errors == 0
    assert not EngineLock.is_taken(app.paths.run_dir / LOCK_NAME)


async def test_a_second_daemon_is_refused_while_one_runs(make_app):
    app = make_app(ScriptedModel([]), config=cfg())
    async with running(app):
        with pytest.raises(AlreadyRunning) as caught:
            await Daemon(app, {}).run()
        assert caught.value.pid is not None


async def test_a_graceful_stop_lets_the_running_run_finish(make_app):
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "slow job"}, call_id="c1")],
            [assistant_message("done")],
        ]
    )
    app = make_app(model, config=cfg(policy=PolicyConfig(allow=["shell"])))
    spy = install(app, Spy(), delay=0.3)
    channel = FakeChannel()
    async with running(app, {"cli": channel}, grace_s=5) as daemon:
        daemon.submit(owner_message("run the slow job"))
        await eventually(lambda: spy.calls, what="the tool to start")
    assert app.inbox.counts()["done"] == 1  # finished before the daemon exited
    assert app.runs.recent()[0].status == "completed"


async def test_a_run_that_outlasts_the_grace_period_is_released_for_the_next_process(make_app):
    model = ScriptedModel([[function_call("shell", {"command": "forever"}, call_id="c1")]])
    app = make_app(
        model,
        config=cfg(
            policy=PolicyConfig(allow=["shell"]),
            runtime=RuntimeConfig(poll_s=0.05, tool_timeout_s=60),
        ),
    )
    spy = install(app, Spy(), delay=30)
    async with running(app, grace_s=0.1) as daemon:
        daemon.submit(owner_message("start something long"))
        await eventually(lambda: spy.calls, what="the tool to start")
    event = app.inbox.get(1)
    assert event["status"] == "pending" and event["attempts"] == 0  # nothing was used up
    assert app.runs.recent()[0].status == "cancelled"


async def test_approvals_nobody_answers_are_closed_by_housekeeping(make_app, clock, monkeypatch):
    monkeypatch.setattr(daemon_module, "HOUSEKEEPING_S", 0.05)
    model = ScriptedModel(
        [
            [function_call("shell", {"command": "git push"}, call_id="c1")],
            [assistant_message("ok, skipped")],
        ]
    )
    app = make_app(model, config=cfg())
    spy = install(app, Spy())
    channel = FakeChannel()
    async with running(app, {"cli": channel}) as daemon:
        daemon.submit(owner_message("push"))
        await eventually(lambda: app.approvals.pending(), what="the approval request")
        clock.advance(13 * 3600)
        await eventually(lambda: app.approvals.recent()[0].status == "expired", what="expiry")
        await eventually(lambda: "ok, skipped" in channel.sent, what="the run to resume")
    assert spy.ran() == []
    assert daemon.stats.approvals_expired == 1


async def test_a_crashing_service_is_restarted_and_does_not_take_the_daemon_down(
    make_app, monkeypatch
):
    class Flaky:
        name = "flaky"

        def __init__(self) -> None:
            self.starts = 0
            self.up = asyncio.Event()

        async def run(self, daemon) -> None:
            self.starts += 1
            if self.starts == 1:
                raise RuntimeError("boom")
            self.up.set()
            await asyncio.Event().wait()

    service = Flaky()
    monkeypatch.setattr(daemon_module.asyncio, "sleep", _fast_sleep(daemon_module.asyncio.sleep))
    app = make_app(ScriptedModel([[assistant_message("still here")]]), config=cfg())
    channel = FakeChannel()
    async with running(app, {"cli": channel}, services=[service]) as daemon:
        await asyncio.wait_for(service.up.wait(), 3)
        daemon.submit(owner_message("ping"))
        await eventually(lambda: channel.sent, what="the reply")
    assert service.starts == 2 and daemon.stats.errors == 1


def _fast_sleep(real):
    async def sleep(seconds, *a):
        await real(min(seconds, 0.01), *a)

    return sleep


async def test_the_heartbeat_tells_other_processes_the_daemon_is_alive(make_app):
    app = make_app(ScriptedModel([]), config=cfg())
    async with running(app):
        beat = app.db.kv_get(HEARTBEAT_KEY)
    assert beat is not None and beat.split()[1] == str(app.clock.now())


# ---- the CLI engine -----------------------------------------------------------------------------
async def test_a_lone_cli_runs_the_agent_inline(make_app):
    app = make_app(ScriptedModel([[assistant_message("inline answer")]]), config=cfg())
    engine = Engine.acquire(app)
    try:
        assert engine.inline
        app.inbox.add(owner_message("hi"))
        [result] = await engine.settle("main")
        assert result.outcome.final_text == "inline answer"
    finally:
        engine.close()
    assert not EngineLock.is_taken(app.paths.run_dir / LOCK_NAME)


async def test_with_a_daemon_running_the_cli_only_submits_and_waits(make_app):
    model = ScriptedModel([[assistant_message("daemon answer")]])
    app = make_app(model, config=cfg())
    channel = FakeChannel()
    async with running(app, {}) as daemon:  # no cli channel: replies wait for the terminal
        client = Engine.acquire(app)
        assert not client.inline and client.holder_pid is not None
        app.inbox.add(owner_message("hi"))
        daemon.wake()
        ticks = 0

        async def tick():
            nonlocal ticks
            ticks += 1

        assert await client.settle("main", tick=tick) == []
        assert ticks >= 1
        assert app.inbox.get(1)["status"] == "done"
        [reply] = app.outbox.list()
        assert (
            reply.text == "daemon answer" and reply.status == "pending"
        )  # for the terminal to show
    assert channel.sent == []


async def test_the_cli_does_not_wait_for_events_that_are_only_waiting_to_retry(make_app, clock):
    app = make_app(ScriptedModel([]), config=cfg())
    lock = EngineLock(app.paths.run_dir / LOCK_NAME)
    assert lock.acquire()  # pretend a daemon is running
    try:
        client = Engine.acquire(app)
        assert not client.inline
        app.inbox.add(owner_message("hi"))
        batch = app.inbox.claim("daemon")  # the daemon picked it up, it failed, retry in 10 minutes
        app.inbox.fail(batch, "no network", environmental=True, retry_in=600)
        assert app.inbox.active_for("main") == 0
        assert await asyncio.wait_for(client.settle("main"), 1) == []
    finally:
        lock.release()
