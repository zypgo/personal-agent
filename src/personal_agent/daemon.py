"""The always-on loop, and the engine a one-off command uses to run the agent.

The agent sleeps most of the time. This process does a few cheap things in a loop:

- workers claim ready events from the inbox and run the harness (a few sessions at once, one run
  per session at a time);
- the scheduler turns due timers into events (and, once a night, queues the memory tidy-up);
- the dispatcher delivers outbox messages (quiet hours and rate limits are applied there);
- housekeeping closes approvals nobody answered and writes a heartbeat;
- once a day the upkeep removes finished records that are past their retention (maintenance.py),
  and every hour it cuts back the logs that nothing else rotates (logs.py).

Nothing is held in memory that the database does not also hold, so killing the process at any
moment loses nothing: leases expire, interrupted runs are repaired by the next run, and
messages whose delivery is unclear are surfaced instead of resent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from personal_agent.channels.base import Channel
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import AddResult, NewEvent
from personal_agent.core.worker import Worker, WorkResult
from personal_agent.harness.dreaming import Dreamer
from personal_agent.logs import trim_logs
from personal_agent.presence import (
    HEARTBEAT_KEY,
    HOUSEKEEPING_S,
    LOCK_NAME,
    AlreadyRunning,
    EngineLock,
)

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

UPKEEP_CHECK_S = 60.0
"""How often the running agent looks whether the daily upkeep is due."""
LOG_CHECK_S = 3600.0
"""How often it looks whether a log that nothing rotates has grown too long."""
ARTIFACT_CHECK_S = 86400.0
"""File retention is independent of whether old database rows are automatically pruned."""


class _Wakeups:
    """Lets any part of the process wake every idle loop at once (new event, new message)."""

    def __init__(self) -> None:
        self._events: set[asyncio.Event] = set()

    @contextlib.contextmanager
    def listener(self) -> Iterator[asyncio.Event]:
        event = asyncio.Event()
        self._events.add(event)
        try:
            yield event
        finally:
            self._events.discard(event)

    def fire(self) -> None:
        for event in list(self._events):
            event.set()


class Service(Protocol):
    """Something that runs next to the loops for as long as the daemon does: a chat-app poller,
    the web server. It is restarted with backoff if it crashes, and cancelled on shutdown."""

    name: str

    async def run(self, daemon: Daemon) -> None: ...


@dataclass
class Stats:
    batches: int = 0
    timers_fired: int = 0
    approvals_expired: int = 0
    messages_sent: int = 0
    rows_pruned: int = 0
    errors: int = 0
    started_at: float = 0.0
    extra: dict[str, int] = field(default_factory=dict)


class Daemon:
    def __init__(
        self,
        app: App,
        channels: dict[str, Channel],
        *,
        services: Sequence[Service] = (),
        grace_s: float = 30.0,
    ) -> None:
        self.app = app
        self.channels = channels
        self.services = list(services)
        if app.collaboration is not None and app.collaboration.root is app:
            self.services.append(app.collaboration)
        self.grace_s = grace_s
        self.poll_s = app.config.runtime.poll_s
        self.dispatcher = Dispatcher(app, channels)
        self.stats = Stats()
        self.lock = EngineLock(app.paths.run_dir / LOCK_NAME)
        self._wake = _Wakeups()
        self._stopping = False
        self._ready = asyncio.Event()

    # ---- control ----------------------------------------------------------------------
    def wake(self) -> None:
        """Something new is waiting: every idle loop checks right now instead of at its next tick."""
        self._wake.fire()

    @property
    def stopping(self) -> bool:
        return self._stopping

    def stop(self) -> None:
        self._stopping = True
        self._wake.fire()

    def submit(self, event: NewEvent) -> AddResult:
        """The way channels hand work to the agent: records the event durably, then wakes a worker."""
        result = self.app.inbox.add(event)
        self.wake()
        return result

    async def wait_ready(self) -> None:
        await self._ready.wait()

    # ---- the loop ---------------------------------------------------------------------
    async def run(self) -> None:
        if not self.lock.acquire():
            raise AlreadyRunning(self.lock.holder_pid())
        tasks: list[asyncio.Task[Any]] = []
        try:
            report = self.app.recover()
            if any(report.values()):
                log.info("recovered after an unclean stop: %s", report)
            await self.app.warm_up()
            self.stats.started_at = self.app.clock.now()
            self._beat()
            tasks = [
                asyncio.create_task(self._worker_loop(i), name=f"worker-{i}")
                for i in range(self.app.config.runtime.concurrency)
            ]
            tasks += [
                asyncio.create_task(self._scheduler_loop(), name="scheduler"),
                asyncio.create_task(self._dispatch_loop(), name="dispatcher"),
                asyncio.create_task(self._housekeeping_loop(), name="housekeeping"),
                asyncio.create_task(self._upkeep_loop(), name="upkeep"),
            ]
            tasks += [
                asyncio.create_task(self._supervise(s), name=f"service-{s.name}")
                for s in self.services
            ]
            if self.app.mcp.active:
                tasks.append(asyncio.create_task(self._start_mcp(), name="mcp-start"))
            self._ready.set()
            log.info("daemon started (pid %s)", os.getpid())
            with self._wake.listener() as event:
                while not self._stopping:
                    event.clear()
                    await self._nap(event, 3600)
        finally:
            # Cancellation and a failed service startup take the same shutdown path as stop().
            # Releasing the engine lock while its workers survive would let a replacement
            # recover their leases and execute the same work concurrently.
            self.stop()
            shutdown = asyncio.create_task(
                self._finish_shutdown(tasks), name=f"shutdown-{self.app.space_id}"
            )
            cancelled = False
            while True:
                try:
                    # A parent may cancel a child that already entered its own finally block.
                    # The engine lock remains held until that child's workers actually stop.
                    await asyncio.shield(shutdown)
                    break
                except asyncio.CancelledError:
                    if shutdown.done():
                        raise
                    cancelled = True
            if cancelled:
                raise asyncio.CancelledError

    async def _finish_shutdown(self, tasks: list[asyncio.Task[Any]]) -> None:
        try:
            await self._shutdown(tasks)
        finally:
            try:
                await self.app.stop_services()
            finally:
                self.lock.release()
                self._ready.clear()
                log.info("daemon stopped")

    async def _start_mcp(self) -> None:
        """Connect the MCP servers in the background, so the first run does not wait for them."""
        try:
            await self.app.mcp.prepare()
        except Exception:
            log.warning("could not start the MCP servers", exc_info=True)

    async def _shutdown(self, tasks: list[asyncio.Task[Any]]) -> None:
        workers = [t for t in tasks if (t.get_name() or "").startswith(("worker-", "upkeep"))]
        if workers:
            # Let runs that are in flight finish; anything slower is cancelled, which releases its
            # events untouched (no attempt used) for the next process. The upkeep stops between two
            # batches and is waited for too: the database must not be closed under it.
            _, pending = await asyncio.wait(workers, timeout=self.grace_s)
            for task in pending:
                task.cancel()
        for task in tasks:
            if not task.done() and task not in workers:
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                log.warning("task ended with an error: %r", result)

    async def _nap(self, event: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(event.wait(), seconds)

    # ---- loops ------------------------------------------------------------------------
    async def _worker_loop(self, index: int) -> None:
        worker = Worker(self.app, owner=f"daemon-{os.getpid()}-{index}")
        with self._wake.listener() as event:
            while not self._stopping:
                event.clear()
                try:
                    if self.app.inbox.has_ready() or self.app.inbox.has_unreported_dead():
                        result = await worker.run_once()
                        if result is not None:
                            self._after_batch(result)
                            continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.stats.errors += 1
                    log.exception("worker %s failed unexpectedly", index)
                await self._nap(event, self.poll_s)

    def _after_batch(self, result: WorkResult) -> None:
        self.stats.batches += 1
        self.wake()  # the run may have queued messages or follow-up events

    async def _scheduler_loop(self) -> None:
        with self._wake.listener() as event:
            while not self._stopping:
                event.clear()
                try:
                    fired = self.app.timers.tick()
                except Exception:
                    self.stats.errors += 1
                    log.exception("scheduler tick failed")
                else:
                    if fired:
                        self.stats.timers_fired += len(fired)
                        self.wake()
                try:
                    if Dreamer(self.app).tick() is not None:
                        self.wake()
                except Exception:
                    self.stats.errors += 1
                    log.exception("the nightly memory pass could not be queued")
                await self._nap(event, self.poll_s)

    async def _dispatch_loop(self) -> None:
        channels = set(self.channels)
        with self._wake.listener() as event:
            while not self._stopping:
                event.clear()
                try:
                    if channels and self.app.outbox.has_due(channels):
                        report = await self.dispatcher.deliver_once()
                        self.stats.messages_sent += len(report.sent)
                        if report.sent:
                            continue
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.stats.errors += 1
                    log.exception("delivery failed unexpectedly")
                await self._nap(event, self.poll_s)

    async def _housekeeping_loop(self) -> None:
        with self._wake.listener() as event:
            while not self._stopping:
                event.clear()
                try:
                    expired = self.app.approvals.expire_due()
                    if expired:
                        self.stats.approvals_expired += len(expired)
                        self.wake()
                    if self.app.jobs is not None:
                        self.app.jobs.reconcile()
                    self._beat()
                except Exception:
                    self.stats.errors += 1
                    log.exception("housekeeping failed")
                await self._nap(event, HOUSEKEEPING_S)

    async def _upkeep_loop(self) -> None:
        """Once a day, remove finished records past their retention; once an hour, cut back the
        logs nothing rotates. It runs on a thread, in small batches, so that nothing else waits for
        it for long, and apart from housekeeping so that the heartbeat does not stop while it works."""
        logs_checked = float("-inf")
        artifacts_checked = float("-inf")
        with self._wake.listener() as event:
            while not self._stopping:
                event.clear()
                try:
                    now = self.app.clock.now()
                    if now - logs_checked >= LOG_CHECK_S:
                        logs_checked = now
                        cut = await asyncio.to_thread(trim_logs, self.app.paths)
                        if cut:
                            log.info("cut back logs that nothing rotates: %s", cut)
                    if now - artifacts_checked >= ARTIFACT_CHECK_S or now < artifacts_checked:
                        reclaimed = await asyncio.to_thread(self.app.artifacts.sweep)
                        artifacts_checked = now
                        if reclaimed:
                            log.info(
                                "artifact upkeep removed %d expired/uncommitted file(s)", reclaimed
                            )
                    if self.app.janitor.due():
                        tidy = await asyncio.to_thread(
                            self.app.janitor.maintain, stop=lambda: self._stopping
                        )
                        self.stats.rows_pruned += tidy.total
                        if tidy.total:
                            log.info(
                                "database upkeep removed %d old record(s): %s",
                                tidy.total,
                                tidy.removed,
                            )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.stats.errors += 1
                    log.exception("database upkeep failed")
                await self._nap(event, UPKEEP_CHECK_S)

    def _beat(self) -> None:
        now = self.app.clock.now()
        self.app.db.kv_set(HEARTBEAT_KEY, f"{os.getpid()} {now}", now=now)

    async def _supervise(self, service: Service) -> None:
        backoff = 1.0
        while not self._stopping:
            try:
                await service.run(self)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                self.stats.errors += 1
                log.exception("service %s crashed; restarting in %.0fs", service.name, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)


# ---- who runs the agent for a CLI command --------------------------------------------------
class Engine:
    """Either this process runs the agent (inline), or an already-running daemon does and this
    process only submits events and shows what comes back (client mode)."""

    def __init__(self, app: App, lock: EngineLock | None, holder_pid: int | None = None) -> None:
        self.app = app
        self.lock = lock
        self.holder_pid = holder_pid
        self.keep_services = False
        """A long conversation keeps MCP servers running between messages; one-off commands do not."""

    @property
    def inline(self) -> bool:
        return self.lock is not None

    @classmethod
    def acquire(cls, app: App) -> Engine:
        lock = EngineLock(app.paths.run_dir / LOCK_NAME)
        if lock.acquire():
            app.recover()
            return cls(app, lock)
        return cls(app, None, lock.holder_pid())

    def close(self) -> None:
        if self.lock is not None:
            self.lock.release()

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    async def settle(
        self,
        session_key: str,
        *,
        tick: Callable[[], Awaitable[None]] | None = None,
        on_failure: str = "drop",
        poll_s: float = 0.2,
    ) -> list[WorkResult]:
        """Get everything ready in a session processed.

        Inline: run it here. Client mode: wait for the daemon to do it, calling `tick` (deliver
        messages) as time passes. Events waiting to retry later are not waited for.
        """
        if self.inline:
            try:
                await self.app.warm_up()
                worker = Worker(self.app)
                results: list[WorkResult] = []
                while (
                    result := await worker.run_once(session_key=session_key, on_failure=on_failure)  # type: ignore[arg-type]
                ) is not None:
                    results.append(result)
                return results
            finally:
                if not self.keep_services:
                    await self.app.stop_services()
        while self.app.inbox.active_for(session_key) > 0:
            if tick is not None:
                await tick()
            await asyncio.sleep(poll_s)
        return []
