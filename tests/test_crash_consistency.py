"""Kill the process after every durable write of a scenario, restart it, and check that nothing was
lost and nothing was done twice.

SQLite commits atomically, so a crash can only leave the files in one of the states between two
commits (see `crash.py`). Each scenario below is played once for every such state: the process dies
there, a new one starts on the same files, and the system runs on to the end. What must hold is the
promise the architecture makes:

- an event is never lost: it ends `done`, or `dead` with the owner told;
- the owner gets one answer, not none and not two (a request the agent is blocked on, or an alert,
  may be repeated: a duplicate beats a lost one);
- an action that needs approval runs at most once, and never without it;
- nothing is left half-way: no message stuck in `sending`, no run still waiting for nobody.

Tools that were allowed to run without asking (a web fetch) are the one place where "twice" is
allowed: the model's request for them is only saved after they ran, so a crash in between makes the
retry ask again. That is at-least-once, by design, and the test says so.
"""

from __future__ import annotations

import asyncio
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from crash import Crash, Owner, World, malformed_history, play, settle
from personal_agent.app import App
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.sessions import INTERRUPTED_RESULT


def message(text: str) -> NewEvent:
    return NewEvent(
        source="cli",
        kind=Kind.MESSAGE,
        session_key="main",
        payload={"text": text},
        external_id="owner-1",
    )


def asks(text: str, *, approves: bool | None = None) -> Callable[[World], Owner]:
    """The owner writes `text` (again every round, like a phone app that resends what nobody
    acknowledged) and, once a request for approval has *reached them*, answers it."""

    def build(world: World) -> Owner:
        def act(app: App) -> None:
            app.inbox.add(message(text))
            if approves is None:
                return
            seen = {
                approval_id
                for sent in world.channel.sent
                if sent.kind == "approval_request"
                for approval_id in sent.payload.get("approval_ids", [])
            }
            for approval in app.approvals.pending():
                if approval.id in seen:
                    app.approvals.decide(approval.id, approves, by="owner")

        return act

    return build


def reminder(world: World) -> Owner:
    """A one-off timer that is due a minute from now, set up by the owner."""
    armed: list[bool] = []

    def act(app: App) -> None:
        if app.timers.get("brief") is None:
            app.timers.add_followup(
                name="brief", prompt="send a brief", due_at=world.clock.now() + 60
            )
        if not armed:
            world.clock.advance(61)
            armed.append(True)

    return act


# ---- what must hold at the end, whatever happened on the way --------------------------------------
def holds_always(world: World, app: App) -> None:
    inbox = app.inbox.counts()
    assert inbox["pending"] == inbox["leased"] == inbox["dead"] == 0, f"inbox: {inbox}"
    assert inbox["done"] >= 1, "the event was never handled"
    outbox = app.outbox.counts()
    assert not (outbox["pending"] or outbox["sending"] or outbox["failed"]), f"outbox: {outbox}"
    assert app.approvals.pending() == [], "a request for approval is still waiting"
    assert app.db.scalar("SELECT COUNT(*) FROM run_states") == 0, "a suspended run was left behind"
    runs = app.runs.summary()
    assert not (runs.get("running") or runs.get("awaiting_approval")), f"runs: {runs}"
    assert malformed_history(world.model_inputs) == []


def answered_once(world: World, app: App, deaths: int) -> None:
    assert world.channel.texts("message") == ["Hello."]
    assert app.outbox.counts()["maybe_sent"] == 0


def fetched(world: World, app: App, deaths: int) -> None:
    assert world.channel.texts("message") == ["Done."]
    ran = len(world.spy.ran("fetch"))
    assert 1 <= ran <= 1 + deaths, f"ran {ran} times after {deaths} crash(es)"


def ran_once_with_approval(world: World, app: App, deaths: int) -> None:
    assert world.channel.texts("message") == ["Done."]
    ran = world.spy.ran("shell")
    assert len(ran) <= 1, f"an approved action ran {len(ran)} times"
    if ran:
        assert world.channel.texts("approval_request"), (
            "it ran, but the owner never saw the request"
        )
        return
    # A crash around the moment of execution: the action was never started, or it is unknown. Either
    # way the model must have been told, so that it can check and ask again.
    outputs = [
        item["output"]
        for item in app.sessions.items("main")
        if item.get("type") == "function_call_output"
    ]
    assert any(INTERRUPTED_RESULT in text for text in outputs), "the model was not told"


def never_ran(world: World, app: App, deaths: int) -> None:
    assert world.channel.texts("message") == ["Done."]
    assert world.spy.ran("shell") == []


def briefed_once_at_most(world: World, app: App, deaths: int) -> None:
    timer = app.timers.get("brief")
    assert timer is not None and timer.status == "fired" and timer.fire_count == 1
    delivered = world.channel.texts("message")
    parked = app.outbox.counts()["maybe_sent"]
    assert len(delivered) + parked == 1, f"delivered {delivered}, parked {parked}"


@dataclass(frozen=True)
class Scenario:
    owner: Callable[[World], Owner]
    check: Callable[[World, App, int], None]
    """Called with the world, the final process and how many processes died."""


SCENARIOS = {
    "chat": Scenario(asks("hi there"), answered_once),
    "fetch": Scenario(asks("please look it up"), fetched),
    "approved": Scenario(asks("please run ls", approves=True), ran_once_with_approval),
    "rejected": Scenario(asks("please run ls", approves=False), never_ran),
    "timer": Scenario(reminder, briefed_once_at_most),
}


async def run_scenario(name: str, die_at: int | str | list[int | str] | None) -> int:
    """Play a scenario with crashes at the given points and check it. Returns how many died."""
    scenario = SCENARIOS[name]
    with tempfile.TemporaryDirectory() as root:
        world = World(home=Path(root) / "home")
        try:
            app, deaths = await play(world, scenario.owner(world), die_at=die_at)
            holds_always(world, app)
            scenario.check(world, app, deaths)
        except AssertionError as exc:
            first = die_at if isinstance(die_at, int) else (die_at or [0])[0]
            exc.add_note(
                f"died after durable write {die_at}; the first one was {world.where(first)}"
            )
            raise
        finally:
            world.close()
    return deaths


@cache
def writes_in(name: str) -> int:
    """How many durable writes the scenario makes when nothing goes wrong."""
    with tempfile.TemporaryDirectory() as root:
        world = World(home=Path(root) / "home")
        try:

            async def count() -> int:
                app = world.boot()
                await settle(app, world, SCENARIOS[name].owner(world))
                connection: Any = app.db._conn
                return int(connection.durable)

            return asyncio.run(count())
        finally:
            world.close()


def crash_points(name: str, *, stride: int = 1) -> list[int]:
    return list(range(1, writes_in(name) + 1, stride))


def one_crash() -> list[Any]:
    return [
        pytest.param(name, n, id=f"{name}-{n}") for name in SCENARIOS for n in crash_points(name)
    ]


# ---- one crash, anywhere ------------------------------------------------------------------------
@pytest.mark.parametrize(("name", "die_at"), one_crash())
async def test_a_crash_anywhere_loses_nothing_and_repeats_nothing(name, die_at):
    assert await run_scenario(name, die_at) == 1


def test_the_scenarios_are_long_enough_to_mean_something():
    """If the system ever stops writing, the crash points above stop covering anything."""
    assert writes_in("chat") >= 10
    assert writes_in("approved") >= 25
    assert writes_in("timer") >= 10


# ---- several crashes in a row -------------------------------------------------------------------
def two_crashes() -> list[Any]:
    return [
        pytest.param(name, [first, second], id=f"{name}-{first}-then-{second}")
        for name in ("chat", "approved")
        for first in crash_points(name, stride=3)
        for second in (1, 2, 4, 8, 16)
    ]


@pytest.mark.parametrize(("name", "die_at"), two_crashes())
async def test_the_recovery_itself_can_be_interrupted(name, die_at):
    await run_scenario(name, die_at)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    name=st.sampled_from(sorted(SCENARIOS)),
    points=st.lists(st.integers(min_value=1, max_value=40), min_size=1, max_size=3),
)
async def test_any_sequence_of_crashes_is_survived(name, points):
    """Three crashes still leave the event its fourth and last attempt (see `Inbox.max_attempts`)."""
    await run_scenario(name, points)


async def test_a_crash_loop_ends_with_the_owner_told_not_with_silence():
    """A message that kills the process on every attempt is given up on, and the owner is told."""
    with tempfile.TemporaryDirectory() as root:
        world = World(home=Path(root) / "home")
        try:
            owner = asks("hi there")(world)
            app, deaths = await play(world, owner, die_at=["status = 'leased'"] * 4)
            assert deaths == 4
            assert app.inbox.counts()["dead"] == 1
            assert world.channel.texts("notice") == ["这条消息我没能处理，只好放弃了。请再发一次。"]
        finally:
            world.close()


# ---- the harness itself -------------------------------------------------------------------------
async def test_a_dead_process_writes_nothing_more(tmp_path):
    world = World(home=tmp_path / "home")
    try:
        app = world.boot(die_at=1)
        with pytest.raises(Crash):
            app.inbox.add(message("first"))
        with pytest.raises(Crash):
            app.inbox.add(message("second"))
        assert world.restart().db.scalar("SELECT COUNT(*) FROM events") == 1
    finally:
        world.close()


async def test_every_durable_write_is_counted_once(tmp_path):
    """A transaction is one durable write however many statements it holds; so is a lone insert."""
    world = World(home=tmp_path / "home")
    try:
        app = world.boot()
        connection: Any = app.db._conn
        before = connection.durable
        with app.db.tx():
            app.db.execute("INSERT INTO kv(key, value, updated_at) VALUES ('a', '1', 0)")
            app.db.execute("INSERT INTO kv(key, value, updated_at) VALUES ('b', '2', 0)")
        assert connection.durable == before + 1
        app.db.execute("INSERT INTO kv(key, value, updated_at) VALUES ('c', '3', 0)")
        assert connection.durable == before + 2
        app.db.query("SELECT * FROM kv")
        assert connection.durable == before + 2
    finally:
        world.close()
