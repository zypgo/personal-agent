"""The web page against a real server inside a real daemon: the event stream, shutdown, and a whole
conversation from a browser's point of view."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass

import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from personal_agent.channels.web import api
from personal_agent.channels.web.channel import WebChannel
from personal_agent.config import Config, RuntimeConfig, WebConfig
from personal_agent.core.approvals import PendingCall
from test_daemon import eventually, running
from test_dispatcher import FakeChannel
from tooling import Spy, install

FAST = RuntimeConfig(poll_s=0.05, concurrency=2)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(api, "TICK_S", 0.05)
    monkeypatch.setattr(api, "PING_S", 0.3)
    monkeypatch.setattr(api, "SESSION_CHECK_S", 0.5)


@dataclass
class Event:
    name: str
    data: dict
    id: str | None = None


class Stream:
    """An open /api/stream, read the way a browser's EventSource does. A background task reads the
    lines, so waiting for the next event with a timeout never cancels the read half-way."""

    def __init__(self, response: httpx.Response) -> None:
        self.comments: list[str] = []
        self.retry: int | None = None
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._pump_task = asyncio.create_task(self._pump(response))
        self._name, self._data, self._id = "message", [], None

    async def _pump(self, response: httpx.Response) -> None:
        try:
            async for line in response.aiter_lines():
                self._queue.put_nowait(line)
        except (httpx.HTTPError, asyncio.CancelledError):
            pass
        finally:
            self._queue.put_nowait(None)

    def close(self) -> None:
        self._pump_task.cancel()

    async def next(self, within: float = 3.0) -> Event | None:
        """The next event; None if the stream ended; TimeoutError if nothing came in time."""
        async with asyncio.timeout(within):
            while True:
                line = await self._queue.get()
                if line is None:
                    self._queue.put_nowait(None)  # stays ended
                    return None
                if line == "":
                    if self._data:
                        event = Event(self._name, json.loads("\n".join(self._data)), self._id)
                        self._name, self._data, self._id = "message", [], None
                        return event
                elif line.startswith(":"):
                    self.comments.append(line)
                elif line.startswith("retry:"):
                    self.retry = int(line.split(":", 1)[1])
                elif line.startswith("event:"):
                    self._name = line.split(":", 1)[1].strip()
                elif line.startswith("data:"):
                    self._data.append(line.split(":", 1)[1].lstrip())
                elif line.startswith("id:"):
                    self._id = line.split(":", 1)[1].strip()

    async def until(self, name: str, within: float = 3.0, *, where=lambda e: True) -> Event:
        async with asyncio.timeout(within):
            while True:
                event = await self.next(within)
                if event is None:
                    raise AssertionError(f"the stream ended before a {name!r} event")
                if event.name == name and where(event):
                    return event


class Browser:
    """An httpx client that behaves like the page: same origin, the page's header, a cookie jar."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.client = httpx.AsyncClient(
            base_url=url, headers={"Origin": url, "X-PA": "1"}, timeout=10.0
        )

    async def pair(self, app, label: str = "test browser") -> None:
        code = app.web_auth.invite(label).code
        response = await self.client.post("/api/pair", json={"code": code})
        assert response.status_code == 200, response.text

    @contextlib.asynccontextmanager
    async def stream(self, *, after: int | None = None, headers: dict | None = None):
        params = {"after": after} if after is not None else {}
        async with self.client.stream(
            "GET", "/api/stream", params=params, headers=headers or {}
        ) as response:
            assert response.status_code == 200, await response.aread()
            assert response.headers["content-type"].startswith("text/event-stream")
            stream = Stream(response)
            try:
                yield stream
            finally:
                stream.close()

    async def send(self, text: str, client_id: str | None = None) -> httpx.Response:
        return await self.client.post(
            "/api/messages",
            data={"text": text, **({"client_id": client_id} if client_id else {})},
            files={"x": (None, "")},
        )

    async def close(self) -> None:
        await self.client.aclose()


@contextlib.asynccontextmanager
async def serving(app):
    web = WebChannel(app, port=0)
    async with running(app, {"cli": FakeChannel(), "web": web}, services=[web]) as daemon:
        await eventually(
            lambda: web.listener is not None and web.listener.started, what="the server"
        )
        browser = Browser(f"http://127.0.0.1:{web.listener.port}")
        try:
            yield browser, daemon, web
        finally:
            await browser.close()


def build(make_app, model=None, **kw):
    kw.setdefault("web", WebConfig(enabled=True))
    return make_app(model, config=Config(runtime=FAST, **kw))


# ---- the stream ------------------------------------------------------------------------------
async def test_a_stream_starts_with_what_the_page_needs_to_catch_up(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            hello = await stream.next()
            assert hello.name == "hello"
            assert hello.data == {"working": False, "last_id": 0, "approvals": []}
            assert stream.retry == 3000  # how soon a dropped connection tries again


async def test_a_new_message_arrives_as_an_event_with_its_id(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            app.transcript.add_in("telegram", "written elsewhere")
            event = await stream.until("turn")
            assert event.data["text"] == "written elsewhere" and event.data["channel"] == "telegram"
            assert event.id == str(event.data["id"])


async def test_a_page_that_reconnects_is_replayed_what_it_missed_and_only_that(make_app):
    app = build(make_app)
    for text in ("one", "two", "three"):
        app.transcript.add_in("web", text)
    first = app.transcript.latest()[0].id
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream(after=first) as stream:
            await stream.until("hello")
            seen = [(await stream.until("turn")).data["text"] for _ in range(2)]
            assert seen == ["two", "three"]
        async with browser.stream(headers={"Last-Event-ID": str(first + 1)}) as stream:
            await stream.until("hello")
            assert (await stream.until("turn")).data["text"] == "three"


async def test_a_page_that_asks_for_nothing_is_not_flooded_with_history(make_app):
    app = build(make_app)
    for i in range(30):
        app.transcript.add_in("web", f"old {i}")
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            app.transcript.add_in("web", "new")
            assert (await stream.until("turn")).data["text"] == "new"


async def test_an_after_beyond_the_end_does_not_skip_what_comes_next(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream(after=10_000) as stream:  # a stale id from another database
            await stream.until("hello")
            app.transcript.add_in("web", "after reset")
            assert (await stream.until("turn")).data["text"] == "after reset"


async def test_the_page_learns_when_the_agent_starts_and_stops_working(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            with app.live.working("main", channel="web"):
                app.live.tool_started("web_search")
                busy = await stream.until(
                    "activity", where=lambda e: e.data["tool"] == "web_search"
                )
                assert busy.data == {"working": True, "tool": "web_search"}
            idle = await stream.until("activity", where=lambda e: not e.data["working"])
            assert idle.data["working"] is False


async def test_a_hello_after_work_began_says_so(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        with app.live.working("main"):
            async with browser.stream() as stream:
                assert (await stream.next()).data["working"] is True


async def test_an_approval_decided_somewhere_else_reaches_the_page(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            [approval] = app.approvals.create(
                "r1", "main", [PendingCall("c1", "shell", "git push")]
            )
            shown = await stream.until("approvals")
            assert [a["status"] for a in shown.data["approvals"]] == ["pending"]
            app.approvals.decide(approval.id, True, by="telegram:7")  # another channel answered
            settled = await stream.until("approvals")
            assert [a["status"] for a in settled.data["approvals"]] == ["approved"]


async def test_a_quiet_stream_still_says_something_now_and_then(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            async with asyncio.timeout(3):
                while not stream.comments:
                    with contextlib.suppress(TimeoutError):
                        await stream.next(0.1)
            assert stream.comments[0] == ": ping"


async def test_ending_the_session_ends_the_stream_with_a_goodbye(make_app, clock):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        [session] = app.web_auth.sessions()
        async with browser.stream() as stream:
            await stream.until("hello")
            app.web_auth.revoke(session.id)
            clock.advance(1)  # the stream looks at its session now and then, by the clock
            await stream.until("signout")
            assert await stream.next() is None  # and it is over


async def test_a_session_that_expires_is_signed_out_of_the_stream_too(make_app, clock):
    app = build(make_app, web=WebConfig(enabled=True, session_days=1))
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            clock.advance(2 * 86400)
            await stream.until("signout")


async def test_a_closed_page_stops_costing_anything(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            assert app.live.listeners == 1
        await eventually(lambda: app.live.listeners == 0, what="the stream to be released")


async def test_stopping_the_daemon_ends_open_streams_promptly(make_app):
    app = build(make_app)
    ended: list[bool] = []
    async with serving(app) as (browser, daemon, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            started = asyncio.get_running_loop().time()
            daemon.stop()
            assert await stream.next(5) is None
            ended.append(asyncio.get_running_loop().time() - started < 3)
    assert ended == [True]


# ---- a whole conversation ------------------------------------------------------------------
async def test_the_owner_asks_in_the_browser_and_the_answer_comes_back_there(make_app):
    model = ScriptedModel([[assistant_message("It is **sunny** in Lisbon.")]])
    app = build(make_app, model)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            sent = await browser.send("how is the weather?", client_id="client-0001")
            assert sent.json()["status"] == "queued"
            mine = await stream.until("turn", where=lambda e: e.data["direction"] == "in")
            assert mine.data["text"] == "how is the weather?" and mine.data["channel"] == "web"
            answer = await stream.until("turn", where=lambda e: e.data["direction"] == "out")
            assert answer.data["text"] == "It is **sunny** in Lisbon."  # the page renders Markdown
            assert answer.data["channel"] == "web"
        again = await browser.client.get("/api/history")
        texts = [t["text"] for t in again.json()["turns"]]
        assert texts == ["how is the weather?", "It is **sunny** in Lisbon."]
    assert [m.status for m in app.outbox.recent()] == ["sent"]


async def test_sending_it_twice_costs_one_answer(make_app):
    model = ScriptedModel([[assistant_message("Done.")]])
    app = build(make_app, model)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        first = await browser.send("do it", client_id="client-0002")
        second = await browser.send("do it", client_id="client-0002")  # the page retried
        assert (first.json()["status"], second.json()["status"]) == ("queued", "duplicate")
        await eventually(lambda: len(app.transcript.latest()) >= 2, what="the answer")
        await asyncio.sleep(0.2)
    assert len(model.calls) == 1
    assert [t.direction for t in app.transcript.latest()] == ["in", "out"]


async def test_an_action_that_needs_a_yes_waits_for_the_page_and_then_goes(make_app):
    push = function_call("shell", {"command": "git push origin main"}, call_id="c1")
    model = ScriptedModel([[push], [assistant_message("Pushed.")]])
    app = build(make_app, model)
    spy = install(app, Spy())
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            await browser.send("push my work", client_id="client-0003")
            asked = await stream.until(
                "turn", where=lambda e: e.data.get("kind") == "approval_request"
            )
            [shown] = asked.data["approvals"]
            assert shown["tool"] == "shell" and shown["status"] == "pending"
            assert spy.ran("shell") == []  # nothing ran yet
            decided = await browser.client.post(
                f"/api/approvals/{shown['id']}", json={"decision": "approve"}
            )
            assert decided.json()["ok"] is True
            await stream.until("turn", where=lambda e: "Pushed." in e.data["text"])
    assert spy.ran("shell") == ["git push origin main"]


async def test_a_denied_action_never_runs(make_app):
    push = function_call("shell", {"command": "git push origin main"}, call_id="c1")
    model = ScriptedModel([[push], [assistant_message("Understood, I did not push.")]])
    app = build(make_app, model)
    spy = install(app, Spy())
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        await browser.send("push my work", client_id="client-0004")
        await eventually(lambda: app.approvals.pending(), what="the request")
        approval = app.approvals.pending()[0]
        response = await browser.client.post(
            f"/api/approvals/{approval.id}", json={"decision": "deny", "note": "not today"}
        )
        assert response.json()["approval"]["status"] == "denied"
        await eventually(
            lambda: any("did not push" in t.text for t in app.transcript.latest()),
            what="the acknowledgement",
        )
    assert spy.ran() == []


async def test_a_command_is_answered_at_once_without_the_model(make_app):
    model = ScriptedModel([])
    app = build(make_app, model)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        async with browser.stream() as stream:
            await stream.until("hello")
            response = await browser.send("/status", client_id="client-0005")
            assert response.json()["status"] == "command"
            reply = await stream.until("turn", where=lambda e: e.data["direction"] == "out")
            assert "Idle." in reply.data["text"]
    assert not model.calls


async def test_the_server_refuses_what_a_hostile_page_would_send(make_app):
    app = build(make_app)
    async with serving(app) as (browser, _, web):
        await browser.pair(app)
        other = (
            f"http://localhost:{web.listener.port + 1}"  # a page on another port of this machine
        )
        rebinding = await browser.client.get("/api/session", headers={"Host": "attacker.example"})
        assert rebinding.status_code == 421
        cross = await browser.client.post("/api/logout", headers={"Origin": other})
        assert cross.status_code == 403
        no_header = await browser.client.post("/api/logout", headers={"X-PA": ""})
        assert no_header.status_code == 403
        assert (await browser.client.get("/api/session")).status_code == 200


async def test_the_address_to_open_is_the_public_one_when_there_is_one(make_app):
    plain = build(make_app)
    assert plain.config.web.address == "http://127.0.0.1:8765"
    public = make_app(
        config=Config(web=WebConfig(enabled=True, public_url="https://mac.tail1234.ts.net"))
    )
    assert public.config.web.address == "https://mac.tail1234.ts.net"


async def test_a_taken_port_is_reported_and_does_not_stop_the_other_channels(make_app, caplog):
    from personal_agent.channels.http import bind

    taken = bind("127.0.0.1", 0)
    taken.listen()
    app = build(make_app)
    cli = FakeChannel()
    web = WebChannel(app, port=taken.getsockname()[1])
    try:
        async with running(app, {"cli": cli, "web": web}, services=[web]) as daemon:
            await asyncio.sleep(0.3)
            assert not daemon.stopping  # the agent carries on without its web page
            app.outbox.enqueue(session_key="main", channel="cli", text="still alive")
            await eventually(lambda: cli.sent, what="a message on the other channel")
    finally:
        taken.close()
    assert "already in use" in caplog.text
