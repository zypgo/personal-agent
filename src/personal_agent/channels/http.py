"""A small web server that lives inside the daemon.

uvicorn is made to be a program of its own. Used as a part of another program, three of its habits
would hurt, so they are switched off here:

- It takes over Ctrl+C and SIGTERM. The daemon already owns them (it must stop in order, letting
  a run in flight finish), so the server never installs a handler.
- A port that is already taken makes it call `sys.exit()`. Inside a task that ends the whole
  event loop, that is, the agent. The socket is bound here instead, and "port in use" is an
  ordinary error that the daemon's supervisor reports and retries.
- On shutdown it waits for every open connection, and a page that is streaming events never
  closes by itself. `stopping` is set first so streams end, and whatever is still open after a
  short grace is cut.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
from collections.abc import Callable, Iterator
from typing import Any

import uvicorn

log = logging.getLogger(__name__)


class PortUnavailable(OSError):
    """The address could not be listened on. The message says what to do about it."""


class _EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield  # the daemon owns the signals


def bind(host: str, port: int) -> socket.socket:
    """A listening socket, or `PortUnavailable`. Port 0 picks a free one (tests)."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    except socket.gaierror as exc:
        raise PortUnavailable(f"cannot listen on {host!r}: {exc}") from None
    family, kind, proto, _, address = infos[0]
    sock = socket.socket(family, kind, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(address)
        sock.setblocking(False)
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            raise PortUnavailable(
                f"port {port} on {host} is already in use (another copy of the agent, or another "
                "program): stop it, or choose another port in config.toml"
            ) from None
        if exc.errno in (errno.EACCES, errno.EPERM):
            raise PortUnavailable(
                f"not allowed to listen on port {port} (use one above 1024)"
            ) from None
        raise PortUnavailable(f"cannot listen on {host}:{port}: {exc.strerror or exc}") from None
    return sock


class HttpListener:
    """One embedded HTTP server. `serve()` runs until it is cancelled (daemon shutdown)."""

    def __init__(
        self,
        *,
        name: str,
        host: str,
        port: int,
        graceful_s: float = 2.0,
        max_connections: int = 100,
        stopping: asyncio.Event | None = None,
        on_stop: Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self.host = host
        self.port = port
        self.graceful_s = graceful_s
        self.max_connections = max_connections
        self.stopping = stopping if stopping is not None else asyncio.Event()
        """Set when the server is shutting down: long-lived responses should end."""
        self.on_stop = on_stop
        self._sock: socket.socket | None = None
        self._server: _EmbeddedServer | None = None

    @property
    def started(self) -> bool:
        return self._server is not None and self._server.started

    def bind(self) -> int:
        """Take the port now (so a conflict is an error before anything else is set up, and port 0
        becomes the real port). Returns the port."""
        if self._sock is None:
            self._sock = bind(self.host, self.port)
            self.port = self._sock.getsockname()[1]
        return self.port

    async def wait_started(self, within: float = 10.0) -> None:
        """Return once connections are being accepted. uvicorn has no readiness callback, so this
        looks; it is for tests and for `pa web open`, never on a hot path."""
        async with asyncio.timeout(within):
            while not self.started:  # noqa: ASYNC110 - nothing to await instead
                await asyncio.sleep(0.01)

    async def serve(self, asgi: Any) -> None:
        self.bind()
        sock, self._sock = self._sock, None
        assert sock is not None
        config = uvicorn.Config(
            asgi,
            log_config=None,
            log_level="warning",
            access_log=False,  # request lines can carry what the owner is looking at
            lifespan="off",
            ws="none",
            http="h11",
            server_header=False,
            timeout_keep_alive=5,
            limit_concurrency=self.max_connections,
        )
        server = self._server = _EmbeddedServer(config)
        task = asyncio.create_task(self._run(server, sock), name=f"http-{self.name}")
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await self._stop(server, task)
            raise
        finally:
            sock.close()
            self._server = None

    def close(self) -> None:
        """Give the port back without serving (a start that went wrong)."""
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    async def _run(self, server: _EmbeddedServer, sock: socket.socket) -> None:
        try:
            await server.serve(sockets=[sock])
        except SystemExit as exc:
            # uvicorn gave up; as a task this would end the event loop, and the agent with it.
            raise RuntimeError(
                f"the {self.name} server could not start (exit {exc.code})"
            ) from None

    async def _stop(self, server: _EmbeddedServer, task: asyncio.Task) -> None:
        self.stopping.set()
        if self.on_stop is not None:
            self.on_stop()
        server.should_exit = True
        _, pending = await asyncio.wait({task}, timeout=self.graceful_s + 1.0)
        if pending:
            server.force_exit = True
            _, pending = await asyncio.wait({task}, timeout=1.0)
        if pending:
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)
