"""The embedded web server, on a real socket: it must behave as part of the daemon, not as a program."""

from __future__ import annotations

import asyncio
import signal
import socket

import httpx
import pytest

from personal_agent.channels.http import HttpListener, PortUnavailable, bind


async def asgi(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["path"] == "/stream":  # never ends on its own, like the page's event stream
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"hello\n", "more_body": True})
        await asyncio.Event().wait()
    body = b"ok"
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"text/plain"), (b"content-length", b"2")],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def started(listener: HttpListener, app=asgi) -> asyncio.Task:
    listener.bind()
    task = asyncio.create_task(listener.serve(app))
    await listener.wait_started(5)
    return task


async def stopped(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


# ---- binding ----------------------------------------------------------------------------------
def test_port_zero_picks_a_free_port():
    sock = bind("127.0.0.1", 0)
    try:
        assert sock.getsockname()[1] > 0
        assert not sock.getblocking()
    finally:
        sock.close()


def test_a_taken_port_is_an_ordinary_error_with_advice():
    first = bind("127.0.0.1", 0)
    first.listen()
    try:
        with pytest.raises(PortUnavailable, match="already in use") as caught:
            bind("127.0.0.1", first.getsockname()[1])
        assert "config.toml" in str(caught.value)
        assert isinstance(caught.value, OSError)
    finally:
        first.close()


def test_a_name_that_does_not_resolve_is_explained():
    with pytest.raises(PortUnavailable, match="cannot listen on"):
        bind("no-such-host.invalid", 8765)


def test_the_listener_reports_the_port_it_got_before_serving():
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    port = listener.bind()
    try:
        assert port > 0 and listener.port == port and listener.bind() == port
        assert not listener.started
    finally:
        listener.close()


def test_closing_gives_the_port_back():
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    port = listener.bind()
    listener.close()
    again = bind("127.0.0.1", port)
    again.close()


# ---- serving ----------------------------------------------------------------------------------
async def test_it_serves_requests_on_the_bound_port():
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    task = await started(listener)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(f"http://127.0.0.1:{listener.port}/")
        assert response.status_code == 200 and response.text == "ok"
        assert "server" not in response.headers  # nothing says what software this is
    finally:
        await stopped(task)


async def test_stopping_gives_the_port_back():
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    task = await started(listener)
    port = listener.port
    await stopped(task)
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))  # not "in use"


async def test_the_daemons_signals_are_left_alone():
    before = (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM))
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    task = await started(listener)
    try:
        assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before
    finally:
        await stopped(task)
    assert (signal.getsignal(signal.SIGINT), signal.getsignal(signal.SIGTERM)) == before


async def test_a_page_that_keeps_a_stream_open_does_not_stop_the_daemon_from_stopping():
    stopping = asyncio.Event()
    told: list[int] = []
    listener = HttpListener(
        name="t",
        host="127.0.0.1",
        port=0,
        graceful_s=0.2,
        stopping=stopping,
        on_stop=lambda: told.append(1),
    )
    task = await started(listener)
    async with (
        httpx.AsyncClient() as client,
        client.stream("GET", f"http://127.0.0.1:{listener.port}/stream") as response,
    ):
        lines = response.aiter_lines()
        assert await asyncio.wait_for(anext(lines), 2) == "hello"
        started_at = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert asyncio.get_running_loop().time() - started_at < 3  # not "forever"
    assert stopping.is_set() and told == [1]


async def test_stopping_tells_long_responses_first_so_they_can_end_themselves():
    stopping = asyncio.Event()
    seen_by_stream: list[bool] = []

    async def polite(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"hi\n", "more_body": True})
        await stopping.wait()  # what the page's event stream does
        seen_by_stream.append(True)
        await send({"type": "http.response.body", "body": b""})

    listener = HttpListener(name="t", host="127.0.0.1", port=0, stopping=stopping, graceful_s=3)
    task = await started(listener, polite)
    async with (
        httpx.AsyncClient() as client,
        client.stream("GET", f"http://127.0.0.1:{listener.port}/") as response,
    ):
        assert await asyncio.wait_for(anext(response.aiter_lines()), 2) == "hi"
        started_at = asyncio.get_running_loop().time()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        # It ended because the stream was told, not because it was cut at the grace limit.
        assert asyncio.get_running_loop().time() - started_at < 2.5
    assert seen_by_stream == [True]


async def test_a_listener_that_cannot_bind_fails_in_serve_without_killing_the_process():
    holder = bind("127.0.0.1", 0)
    holder.listen()
    try:
        listener = HttpListener(name="t", host="127.0.0.1", port=holder.getsockname()[1])
        with pytest.raises(PortUnavailable):
            await listener.serve(asgi)
    finally:
        holder.close()


async def test_a_server_that_gives_up_becomes_an_ordinary_error(monkeypatch):
    # uvicorn ends a failed start with sys.exit(); inside a task that would stop the whole agent.
    import uvicorn

    async def fail(self, sockets=None):
        raise SystemExit(1)

    monkeypatch.setattr(uvicorn.Server, "serve", fail)
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    with pytest.raises(RuntimeError, match="could not start"):
        await listener.serve(asgi)
    listener.close()


async def test_the_request_log_does_not_record_what_the_owner_looks_at(caplog):
    listener = HttpListener(name="t", host="127.0.0.1", port=0)
    task = await started(listener)
    try:
        async with httpx.AsyncClient() as client:
            await client.get(f"http://127.0.0.1:{listener.port}/secret-path?q=private-words")
    finally:
        await stopped(task)
    assert "private-words" not in caplog.text and "secret-path" not in caplog.text
