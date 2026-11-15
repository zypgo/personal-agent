"""The always-on loop, and the lock that says who is running the agent.

The agent sleeps most of the time. This process does four cheap things in a loop:

- workers claim ready events from the inbox and run the harness (a few sessions at once, one run
  per session at a time);
- the scheduler turns due timers into events (and, once a night, queues the memory tidy-up);
- the dispatcher delivers outbox messages (quiet hours and rate limits are applied there);
- housekeeping closes approvals nobody answered and writes a heartbeat.

Nothing is held in memory that the database does not also hold, so killing the process at any
moment loses nothing: leases expire, interrupted runs are repaired by the next run, and
messages whose delivery is unclear are surfaced instead of resent.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from personal_agent.channels.base import Channel
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import AddResult, NewEvent
from personal_agent.core.worker import Worker, WorkResult
from personal_agent.harness.dreaming import Dreamer

if TYPE_CHECKING:
    from personal_agent.app import App

log = logging.getLogger(__name__)

LOCK_NAME = "engine.lock"
HEARTBEAT_KEY = "daemon:heartbeat"
HOUSEKEEPING_S = 15.0


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int | None) -> None:
        super().__init__(f"another process is already running the agent (pid {pid or 'unknown'})")
        self.pid = pid


class EngineLock:
    """Exclusive "I run the agent" lock. Held by the daemon, or by a lone CLI session.

    An advisory `flock`: the kernel releases it when the process dies, so it can never go stale.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)
        self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def holder_pid(self) -> int | None:
        """The pid written by whoever holds the lock (only meaningful while it is held)."""
        try:
            return int(self.path.read_text().strip())
        except (OSError, ValueError):
            return None

    @classmethod
    def is_taken(cls, path: Path) -> bool:
        """True if some process currently holds the lock."""
        probe = cls(path)
        if probe.acquire():
            probe.release()
            return False
        return True


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
            await self._shutdown(tasks)
        finally:
            await self.app.stop_services()  # also on a failed start: never leave programs running
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
        workers = [t for t in tasks if (t.get_name() or "").startswith("worker-")]
        if workers:
            # Let runs that are in flight finish; anything slower is cancelled, which releases its
            # events untouched (no attempt used) for the next process.
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
                    if self.app.inbox.has_ready():
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
                    self._beat()
                except Exception:
                    self.stats.errors += 1
                    log.exception("housekeeping failed")
                await self._nap(event, HOUSEKEEPING_S)

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


@dataclass(frozen=True)
class DaemonState:
    kind: str
    """"daemon" (the background service), "terminal" (a chat or command is running the agent
    itself) or "stopped"."""
    pid: int | None = None
    heartbeat_age_s: float | None = None


def daemon_state(app: App) -> DaemonState:
    lock_path = app.paths.run_dir / LOCK_NAME
    if not EngineLock.is_taken(lock_path):
        return DaemonState("stopped")
    pid = EngineLock(lock_path).holder_pid()
    beat = (app.db.kv_get(HEARTBEAT_KEY) or "").split()
    if len(beat) == 2 and beat[0] == str(pid):
        age = max(0.0, app.clock.now() - float(beat[1]))
        if age < 4 * HOUSEKEEPING_S:
            return DaemonState("daemon", pid, age)
    return DaemonState("terminal", pid)


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
