"""A process that dies between two commits, and the restart that follows.

SQLite makes every commit atomic, so a crash can only leave the database in one of the states
between two commits. This module lets a test stop "the process" right after the Nth commit, start
a new one on the same files, and keep going: running a scenario once for every N covers every
state a real `kill -9` can leave behind.

What the outside world remembers (messages that were sent, commands that ran) lives in `World`
and survives the restart, because a message that reached the owner's phone is not un-sent when
the agent dies.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agents.testing import ModelStep, ScriptedModel, assistant_message, function_call

from personal_agent.app import App, build_app
from personal_agent.clock import ManualClock
from personal_agent.config import Config
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.outbox import OutMessage
from personal_agent.core.worker import Worker
from personal_agent.security.sandbox import SandboxProvider
from tooling import Spy, install

RESTART_S = 2.0
"""How long a restart takes. The new process must not need the dead one's claims to run out (a
lease lasts 300 s): it owns the engine lock, so it knows they are dead."""


class Crash(BaseException):
    """The process died here. A `BaseException`, so that nothing in the program can swallow it."""


class DyingConnection:
    """Stands in for the `sqlite3.Connection` of a `Database`.

    It counts the moments something becomes durable: a `COMMIT`, or a write outside a transaction
    (which SQLite commits by itself). Right after the `die_at`-th of them it raises `Crash`, and
    from then on every call raises it again: a dead process writes nothing more. `die_at` can also
    be some text: the process then dies right after the first durable write whose statements
    contain it ("status = 'leased'" is the moment an event is claimed).
    """

    def __init__(self, conn: Any, die_at: int | str | None) -> None:
        self._conn = conn
        self.die_at = die_at
        self.durable = 0
        self.dead = False
        self.trail: list[str] = []
        """What each durable write changed, in order (for a failing test to say where it died)."""
        self._writes: list[str] = []

    def _after(self, sql: str, was_in_transaction: bool) -> None:
        head = sql.lstrip()[:8].upper()
        is_write = head.startswith(("INSERT", "UPDATE", "DELETE", "REPLACE"))
        if is_write:
            self._writes.append(" ".join(sql.split())[:60])
        autocommitted = not was_in_transaction and is_write
        if head.startswith(("COMMIT", "END")) or autocommitted:
            self.durable += 1
            statements = " | ".join(self._writes)
            self.trail.append(f"{self.durable}: {statements[:150]}")
            self._writes = []
            if self._dies_now(statements):
                self.dead = True
                raise Crash(f"the process died after durable write {self.durable}")

    def _dies_now(self, statements: str) -> bool:
        if isinstance(self.die_at, int):
            return self.durable >= self.die_at
        return self.die_at is not None and self.die_at in statements

    def execute(self, sql: str, params: Any = ()) -> Any:
        if self.dead:
            raise Crash("the process is dead")
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.execute(sql, params)
        self._after(sql, was_in_transaction)
        return cursor

    def executemany(self, sql: str, rows: Any) -> Any:
        if self.dead:
            raise Crash("the process is dead")
        was_in_transaction = self._conn.in_transaction
        cursor = self._conn.executemany(sql, rows)
        self._after(sql, was_in_transaction)
        return cursor

    def close(self) -> None:
        self._conn.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


class RecordingChannel:
    """A channel that remembers what it was asked to deliver. Part of the world: it outlives the
    process, like the owner's phone."""

    def __init__(self, name: str = "cli") -> None:
        self.name = name
        self.sent: list[OutMessage] = []

    async def send(self, message: OutMessage) -> None:
        self.sent.append(message)

    def texts(self, kind: str | None = None) -> list[str]:
        return [m.text for m in self.sent if kind in (None, m.kind)]


def content_model(inputs: list[list[dict[str, Any]]] | None = None) -> ScriptedModel:
    """A model whose answer depends on what is in front of it, not on how many times it was asked,
    so that a retry after a crash gets a sensible answer however far the first attempt got.

    Every request is appended to `inputs` (a snapshot of what the model was shown), so a test can
    check that the history stayed well formed through the crash."""

    def respond(call: Any) -> Any:
        seen = json.dumps(call.input, default=str)
        if inputs is not None:
            inputs.append(json.loads(seen))
        if '"function_call_output"' in seen:
            return [assistant_message("Done.")]
        if "run ls" in seen:
            return [function_call("shell", {"command": "ls"}, call_id="c1")]
        if "look it up" in seen:
            return [function_call("fetch", {"url": "https://example.com"}, call_id="c1")]
        if "send a brief" in seen:
            return [function_call("send_message", {"text": "Your brief."}, call_id="c1")]
        return [assistant_message("Hello.")]

    return ScriptedModel([ModelStep.respond(respond)] * 60)


def malformed_history(inputs: Sequence[Sequence[dict[str, Any]]]) -> list[str]:
    """What the API would refuse in the requests the model was sent: an answer to a call that was
    never made, or two answers to the same call."""
    problems: list[str] = []
    for number, items in enumerate(inputs, 1):
        called: set[str] = set()
        answered: set[str] = set()
        for item in items:
            call_id = item.get("call_id")
            if item.get("type") == "function_call":
                called.add(call_id)
            elif item.get("type") == "function_call_output":
                if call_id not in called:
                    problems.append(
                        f"request {number}: answer to {call_id!r}, which was never called"
                    )
                if call_id in answered:
                    problems.append(f"request {number}: {call_id!r} is answered twice")
                answered.add(call_id)
    return problems


@dataclass
class World:
    """Everything that lives on after the process: the files, the clock, what was delivered and
    what was run."""

    home: Path
    clock: ManualClock = field(default_factory=ManualClock)
    channel: RecordingChannel = field(default_factory=RecordingChannel)
    spy: Spy = field(default_factory=Spy)
    config: Config | None = None
    boots: list[App] = field(default_factory=list)
    model_inputs: list[list[dict[str, Any]]] = field(default_factory=list)

    def boot(self, *, die_at: int | str | None = None) -> App:
        """Start a process on the files. `die_at=N` makes it die after its Nth durable write."""
        app = build_app(
            home=self.home,
            clock=self.clock,
            config=self.config,
            model_factory=lambda name: content_model(self.model_inputs),
            sandbox=SandboxProvider.fixed(None, "tests run without a sandbox"),
            read_env=False,
        )
        install(app, self.spy)
        app.db._conn = DyingConnection(app.db._conn, die_at)
        self.boots.append(app)
        return app

    def restart(self, *, die_at: int | str | None = None) -> App:
        """What the daemon does when it starts after a crash: a new process, which cleans up after
        the old one straight away."""
        self.clock.advance(RESTART_S)
        app = self.boot(die_at=die_at)
        app.recover()
        return app

    def where(self, durable_write: int) -> str:
        """What the first process was writing when it died after its Nth durable write."""
        connection = self.boots[0].db._conn
        assert isinstance(connection, DyingConnection)
        return (
            connection.trail[durable_write - 1] if durable_write <= len(connection.trail) else "?"
        )

    def close(self) -> None:
        for app in self.boots:
            with contextlib.suppress(Exception):  # a process that died may have closed it already
                app.db._conn.close()


Owner = Callable[[App], None]


async def settle(app: App, world: World, owner: Owner, *, rounds: int = 40) -> None:
    """Let the system run until nothing is left to do: the owner acts, the worker runs what is in
    the inbox, the timers fire, the dispatcher delivers. `owner` is called every round and must
    be idempotent (a phone app that resends a message nobody acknowledged)."""
    worker = Worker(app)
    dispatcher = Dispatcher(app, {world.channel.name: world.channel})
    for _ in range(rounds):
        owner(app)
        busy = False
        app.timers.tick()
        while await worker.run_once() is not None:
            busy = True
        report = await dispatcher.deliver_once()
        busy = busy or bool(report.sent or report.failed or report.unknown or report.retrying)
        if not busy and not app.inbox.has_ready() and not app.outbox.has_due():
            return
    raise AssertionError("the system never went quiet")


async def play(
    world: World, owner: Owner, *, die_at: int | str | Sequence[int | str] | None = None
) -> tuple[App, int]:
    """Run the scenario in a process that dies at `die_at` (see `DyingConnection`), restart it, and
    run on to the end. A sequence of crash points kills one process after another (each counts its
    own writes); one that finishes before reaching its point simply survives.
    Returns the final process and how many died on the way."""
    points = [die_at] if isinstance(die_at, (int, str)) else list(die_at or [])
    deaths = 0
    while True:
        point = points.pop(0) if points else None
        try:
            # A process can die while it is cleaning up after the last one, too.
            app = world.restart(die_at=point) if deaths else world.boot(die_at=point)
            await settle(app, world, owner)
            return app, deaths
        except Crash:
            deaths += 1


async def durable_writes(world: World, owner: Owner) -> int:
    """How many durable writes the scenario makes when nothing goes wrong: the number of crash
    points to try."""
    app = world.boot(die_at=None)
    await settle(app, world, owner)
    connection = app.db._conn
    assert isinstance(connection, DyingConnection)
    return connection.durable
