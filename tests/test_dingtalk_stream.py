from __future__ import annotations

import asyncio
import json
from http import HTTPStatus

import httpx
import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import InvalidStatus

from channel_identity_helpers import bind_owner
from personal_agent.channels.dingtalk.api import DingTalkApi, DingTalkApiError
from personal_agent.channels.dingtalk.channel import DingTalkChannel, open_stream
from personal_agent.channels.dingtalk.config import DingTalkConfig
from personal_agent.channels.ingress import Ingress
from personal_agent.config import Config
from test_dingtalk_api import FakeDingTalk
from test_dingtalk_channel import OWNER, AckSocket, frame, incoming
from test_ingress import events


class FakeDaemon:
    stopping = False

    def __init__(self):
        self.woken = 0

    def wake(self):
        self.woken += 1


@pytest.fixture(autouse=True)
def no_proxy_for_our_own_server(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


async def test_real_websocket_reconnects_with_a_fresh_ticket_dedupes_and_closes_on_cancel(
    make_app, monkeypatch
):
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    app.pairing.allow("dingtalk", OWNER)
    bind_owner(app, "dingtalk", OWNER)
    daemon = FakeDaemon()
    fake = FakeDingTalk()
    ready, all_closed = asyncio.Event(), asyncio.Event()
    received, connections, pauses, closed = [], [], [], []

    async def handler(socket):
        index = len(connections)
        connections.append(socket.request.path)
        try:
            await socket.send(frame(incoming(), delivery_id=f"delivery-{index}"))
            received.append(json.loads(await socket.recv()))
            if index == 0:
                await socket.send(frame({"opaque": "ping-value"}, kind="SYSTEM", topic="ping"))
                received.append(json.loads(await socket.recv()))
                await socket.send(
                    json.dumps({"type": "SYSTEM", "headers": {"topic": "disconnect"}, "data": "{}"})
                )
            else:
                ready.set()
            await socket.wait_closed()
        finally:
            closed.append(index)
            if len(closed) == 2:
                all_closed.set()

    async def sleep(delay):
        pauses.append(delay)
        await asyncio.sleep(0)

    async with fake.client() as client, serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        api = DingTalkApi("offline-app", "offline-secret", client=client)
        tickets = []

        async def open_connection():
            tickets.append(len(tickets) + 1)
            return f"ws://127.0.0.1:{port}/connect?ticket=offline-{tickets[-1]}"

        monkeypatch.setattr(api, "open_connection", open_connection)
        channel = DingTalkChannel(app, api=api, sleep=sleep)
        assert channel.health()["state"] == "stopped"  # configuration is not a connection
        task = asyncio.create_task(channel.run(daemon))
        try:
            await asyncio.wait_for(ready.wait(), 3)
            assert channel.health()["state"] == "connected"
            assert len(events(app)) == 1 and daemon.woken == 1
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        await asyncio.wait_for(all_closed.wait(), 3)
    assert tickets == [1, 2] and len(set(connections)) == 2
    assert len(pauses) == 1 and 1 <= pauses[0] <= 1.5
    assert [ack["code"] for ack in received] == [200, 200, 200]
    assert json.loads(received[1]["data"]) == {"opaque": "ping-value"}
    assert channel.health()["state"] == "stopped" and sorted(closed) == [0, 1]


@pytest.mark.parametrize("phase", ["opening", "backoff"])
async def test_cancellation_closes_owned_http_client_during_open_or_backoff(
    make_app, monkeypatch, phase
):
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    monkeypatch.setenv("DINGTALK_CLIENT_ID", "offline-app")
    monkeypatch.setenv("DINGTALK_CLIENT_SECRET", "offline-secret")
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request):
        if phase == "backoff":
            return httpx.Response(503)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def sleep(delay):
        entered.set()
        await asyncio.Event().wait()

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    api = DingTalkApi("offline-app", "offline-secret", client=client)
    monkeypatch.setattr("personal_agent.channels.dingtalk.channel.DingTalkApi", lambda *args: api)
    channel = DingTalkChannel(app, sleep=sleep)
    task = asyncio.create_task(channel.run(FakeDaemon()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        closed_by_channel = client.is_closed
        await client.aclose()  # also closes the test fixture if an assertion above fails
    assert closed_by_channel and channel._api is None and channel.state == "stopped"
    if phase == "opening":
        assert cancelled.is_set()


async def test_permanent_gateway_auth_failure_stops_without_spinning_or_leaking_details(
    make_app, monkeypatch
):
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    fake = FakeDingTalk()
    async with fake.client() as client:
        api = DingTalkApi("offline-app", "offline-secret", client=client)

        async def rejected():
            raise DingTalkApiError("DingTalk refused the request (HTTP 401)", permanent=True)

        monkeypatch.setattr(api, "open_connection", rejected)
        channel = DingTalkChannel(app, api=api)
        await channel.run(FakeDaemon())
        assert channel.health()["state"] == "error"
        assert "offline-secret" not in repr(channel.health())


async def test_socket_http_redirect_is_refused_without_following_the_bearer_ticket():
    followed = []

    async def destination(socket):
        followed.append(socket.request.path)

    async with serve(destination, "127.0.0.1", 0) as target:
        port = target.sockets[0].getsockname()[1]

        def redirect(connection, request):
            response = connection.respond(HTTPStatus.TEMPORARY_REDIRECT, "Redirect")
            response.headers["Location"] = f"ws://127.0.0.1:{port}/connect?ticket=offline-ticket"
            return response

        async with serve(destination, "127.0.0.1", 0, process_request=redirect) as server:
            origin = server.sockets[0].getsockname()[1]
            with pytest.raises(InvalidStatus):
                async with open_stream(f"ws://127.0.0.1:{origin}/connect?ticket=offline-ticket"):
                    pytest.fail("a redirect must not establish a connection")
    assert followed == []


async def test_malformed_and_unknown_frames_are_bounded_and_do_not_enter_ingress(make_app):
    app = make_app(config=Config(dingtalk=DingTalkConfig(enabled=True)))
    channel = DingTalkChannel(app)
    socket = AckSocket()
    ingress = Ingress(app)
    assert await channel._frame("not json", socket, ingress)
    assert await channel._frame('{"type":"CALLBACK","headers":{}}', socket, ingress)
    assert not socket.acks
    assert await channel._frame(frame({}, topic="unknown"), socket, ingress)
    malformed = json.loads(frame())
    malformed["data"] = "not json"
    assert await channel._frame(json.dumps(malformed), socket, ingress)
    assert [ack["code"] for ack in socket.acks] == [404, 400]
    assert not events(app)
