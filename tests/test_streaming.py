"""Real SDK streaming, including interrupted work and an approval resumed after restart."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from agents.testing import ScriptedModel, assistant_message, function_call

from helpers import owner_message, timer
from personal_agent.config import Config, WebConfig
from personal_agent.core.worker import Worker
from test_daemon import eventually
from test_web_channel import build, serving
from tooling import Spy, drain, install


class PausedModel(ScriptedModel):
    """Keep a real SDK model stream open after a visible prefix, without any network request."""

    def __init__(self, *, fail=False):
        super().__init__([[assistant_message("First words and the finished answer.")]])
        self.release = asyncio.Event()
        self.closed = asyncio.Event()
        self.fail = fail

    async def stream_response(self, *args, **kwargs):
        try:
            async for event in super().stream_response(*args, **kwargs):
                if event.type == "response.output_text.delta":
                    yield event.model_copy(update={"delta": "First words"})
                    await self.release.wait()
                    if self.fail:
                        raise httpx.ReadTimeout("the simulated provider stopped mid-response")
                    yield event.model_copy(update={"delta": " and the finished answer."})
                else:
                    yield event
        finally:
            self.closed.set()


def preview(app):
    return next((d for d in app.live.response_drafts() if d["text"]), None)


async def test_public_text_is_visible_before_completion_but_not_committed_as_a_turn(make_app):
    model = PausedModel()
    app = make_app(model, config=Config(web=WebConfig(enabled=True)))
    app.inbox.add(owner_message("Explain", source="web"))
    work = asyncio.create_task(Worker(app).run_once())
    try:
        await eventually(lambda: preview(app), what="a real streamed preview")
        assert preview(app)["text"] == "First words"
        assert not work.done()
        assert not app.outbox.recent()
        assert not [t for t in app.transcript.latest() if t.direction == "out"]
    finally:
        model.release.set()
        result = await work
    assert result.outcome.status == "completed"
    assert model.calls[0].streamed
    assert app.live.response_drafts() == []
    assert [m.text for m in app.outbox.recent()] == ["First words and the finished answer."]


async def test_worker_cancellation_closes_provider_stream_and_removes_the_preview(make_app):
    model = PausedModel()
    app = make_app(model, config=Config(web={"enabled": True}))
    app.inbox.add(owner_message("Explain", source="web"))
    work = asyncio.create_task(Worker(app).run_once())
    try:
        await eventually(lambda: preview(app), what="stream text")
    finally:
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
    assert model.closed.is_set()
    assert app.live.response_drafts() == []
    assert not app.outbox.recent()
    assert len(model.calls) == 1


async def test_failed_stream_never_silently_replays_the_same_request_as_nonstreaming(make_app):
    model = PausedModel(fail=True)
    app = make_app(model, config=Config(web={"enabled": True}))
    app.inbox.add(owner_message("Explain", source="web"))
    work = asyncio.create_task(Worker(app).run_once())
    try:
        await eventually(lambda: preview(app), what="stream text")
    finally:
        model.release.set()
        await work
    assert len(model.calls) == 1 and model.calls[0].streamed
    assert model.closed.is_set()
    assert app.live.response_drafts() == []
    assert not any(m.text.startswith("First words") for m in app.outbox.recent())


@pytest.mark.parametrize(
    ("source", "web_on", "stream_on", "streamed"),
    [
        ("web", True, True, True),
        ("web", True, False, False),
        ("web", False, True, False),
        ("telegram", True, True, False),
        ("dingtalk", True, True, False),
        ("cli", True, True, False),
    ],
)
async def test_only_enabled_interactive_web_runs_stream(
    make_app, source, web_on, stream_on, streamed
):
    model = ScriptedModel([[assistant_message("Finished.")]])
    app = make_app(model, config=Config(web={"enabled": web_on, "stream_responses": stream_on}))
    app.inbox.add(owner_message("Hi", source=source))
    assert (await Worker(app).run_once()).outcome.status == "completed"
    assert model.calls[0].streamed is streamed


async def test_background_work_never_has_answer_previews(make_app):
    model = ScriptedModel([[assistant_message("Nothing new.")]])
    app = make_app(model, config=Config(web={"enabled": True}, notify={"channel": "web"}))
    app.inbox.add(timer("check", "Check for changes"))
    with app.live.subscribe() as events:
        await Worker(app).run_once()
        await asyncio.sleep(0)
        assert all(e["type"] != "drafts" for e in events.drain())
    assert not model.calls[0].streamed


async def test_streamed_tool_approval_survives_restart_and_executes_once(make_app):
    cfg = Config(web={"enabled": True})
    first_model = ScriptedModel(
        [[function_call("shell", {"command": "git push origin main"}, call_id="push1")]]
    )
    app = make_app(first_model, config=cfg)
    before = install(app, Spy())
    app.inbox.add(owner_message("Push my branch", source="web"))
    [first] = await drain(app)
    assert first.outcome.status == "awaiting_approval"
    assert first_model.calls[0].streamed
    assert before.ran() == [] and app.live.response_drafts() == []
    [approval] = app.approvals.pending()
    app.close()

    second_model = ScriptedModel([[assistant_message("Pushed.")]])
    resumed = make_app(second_model, config=cfg)
    after = install(resumed, Spy())
    resumed.recover()
    assert resumed.approvals.decide(approval.id, True, by="owner").resume_queued
    [second] = await drain(resumed)
    assert second.outcome.status == "completed"
    assert second.outcome.run_id == first.outcome.run_id
    assert second_model.calls[0].streamed
    assert after.ran("shell") == ["git push origin main"]
    assert resumed.approvals.load_run(first.outcome.run_id) is None
    assert resumed.live.response_drafts() == []


async def test_phone_reconnect_gets_preview_snapshot_then_one_durable_answer(make_app, monkeypatch):
    from personal_agent.channels.web import api

    monkeypatch.setattr(api, "TICK_S", 0.05)
    model = PausedModel()
    app = build(make_app, model)
    async with serving(app) as (browser, _, _):
        await browser.pair(app)
        try:
            async with browser.stream(after=0) as stream:
                await stream.until("hello")
                assert (await browser.send("Explain")).status_code == 200
                draft_event = await stream.until(
                    "drafts", where=lambda e: any(d["text"] for d in e.data["drafts"])
                )
                assert draft_event.id is None
                assert draft_event.data["drafts"][0]["text"] == "First words"
            assert not model.closed.is_set()  # disconnecting a phone does not cancel the worker
            async with browser.stream(after=0) as stream:
                hello = await stream.until("hello")
                assert hello.data["drafts"][0]["text"] == "First words"
                assert hello.id is None
                model.release.set()
                final = await stream.until("turn", where=lambda e: e.data["direction"] == "out")
                assert final.data["text"] == "First words and the finished answer."
                assert final.id == str(final.data["id"])
                assert final.data["meta"]["run_id"] == hello.data["drafts"][0]["run_id"]
                assert final.data["meta"]["final_response"] is True
        finally:
            model.release.set()
    assert len(model.calls) == 1
    assert len([t for t in app.transcript.latest() if t.direction == "out"]) == 1
