"""The same published bytes reach authenticated Web downloads and Telegram documents."""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from channel_identity_helpers import bind_owner, revoke_owner
from personal_agent.channels.telegram.api import TelegramApi
from personal_agent.channels.telegram.channel import TelegramChannel
from personal_agent.channels.web.channel import WebChannel
from personal_agent.config import Config, NotifyConfig
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.db import dumps
from test_artifacts import publish, source
from web_fake import Web


def test_an_artifact_id_is_not_a_bearer_credential(make_app):
    site = Web(make_app())
    source(site.app)
    artifact = publish(site.app).artifact
    path = f"/api/artifacts/{artifact.id}"
    assert site.client.get(path).status_code == 401
    site.client.cookies.set("pa_session", "forged")
    assert site.client.get(path).status_code == 401
    site.pair()
    assert site.client.get(path).content == b"a finished report"
    site.client.post("/api/logout", headers=site.write)
    assert site.client.get(path).status_code == 401


@pytest.mark.parametrize("filename", ["report.html", "diagram.svg", "results.pdf", "报告.txt"])
def test_active_content_is_only_an_authenticated_download(make_app, filename):
    site = Web(make_app())
    site.pair()
    source(site.app, filename, b"<script>fetch('/api/session')</script>")
    artifact = publish(site.app, filename).artifact
    response = site.client.get(f"/api/artifacts/{artifact.id}")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "private, no-store"


def test_downloads_reject_expired_missing_and_tampered_files(make_app):
    site = Web(make_app())
    site.pair()
    source(site.app)
    artifact = publish(site.app).artifact
    assert site.client.get("/api/artifacts/not-an-id").status_code == 404
    assert site.client.get("/api/artifacts/" + "0" * 32).status_code == 404
    blob = site.app.paths.artifacts_dir / f"{artifact.id}.blob"
    blob.write_bytes(b"wrong snapshot")
    assert site.client.get(f"/api/artifacts/{artifact.id}").status_code == 404
    site.app.clock.advance(30 * 86400)
    site.pair()
    assert site.client.get(f"/api/artifacts/{artifact.id}").status_code == 410


async def test_web_delivery_enters_history_with_stable_metadata(make_app):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    report = await Dispatcher(app, {"web": WebChannel(app)}).deliver_once()
    assert report.sent == [artifact.outbox_id]
    site = Web(app)
    site.pair()
    response = site.client.get("/api/history")
    assert response.status_code == 200
    [turn] = app.transcript.latest()
    assert turn.kind == "artifact"
    assert turn.meta["artifacts"] == [artifact.to_json()]
    assert turn.meta["run_id"] == "run:1"
    assert artifact.id in response.text
    assert "source_path" not in response.text


class DocumentApi:
    """An actual TelegramApi over a mock HTTP transport, including multipart encoding."""

    def __init__(self, response=200):
        self.response = response
        self.calls = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        self.api = TelegramApi("12345:offline-test-token", client=self.client)

    def handle(self, request):
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"id": 1, "is_bot": True}})
        self.calls.append((request.url.path, request.headers, request.content))
        if isinstance(self.response, Exception):
            raise self.response
        if self.response == 200:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 8}})
        return httpx.Response(
            self.response,
            json={"ok": False, "description": "mock refusal", "parameters": {"retry_after": 17}},
        )


async def deliver(app, response=200):
    transport = DocumentApi(response)
    app.pairing.allow("telegram", "7", "owner")
    bind_owner(app, "telegram", "7")
    channel = TelegramChannel(app, api=transport.api)
    try:
        report = await Dispatcher(app, {"telegram": channel}).deliver_once()
    finally:
        await transport.client.aclose()
    return report, transport.calls


async def test_telegram_uploads_exactly_one_document_with_its_caption(make_app):
    app = make_app()
    source(app, content=b"binary\x00file\xffbytes")
    artifact = publish(
        app, channel="telegram", recipient="7", description="The requested file."
    ).artifact
    report, calls = await deliver(app)
    assert report.sent == [artifact.outbox_id]
    [(path, headers, body)] = calls
    assert path.endswith("/sendDocument")
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b"binary\x00file\xffbytes" in body
    assert b'filename="report.txt"' in body
    assert b"The requested file." in body
    assert b'name="chat_id"\r\n\r\n7' in body
    [turn] = app.transcript.latest()
    assert turn.meta["artifacts"] == [artifact.to_json()]


@pytest.mark.parametrize("response", [400, 401, 403, 404, 413, 422])
async def test_definite_upload_refusals_fail_without_claiming_delivery(make_app, response):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="7").artifact
    report, calls = await deliver(app, response)
    assert report.failed == [artifact.outbox_id]
    assert app.outbox.get(artifact.outbox_id).status == "failed"
    assert app.transcript.latest() == []
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        429,
        httpx.ConnectError("no connection"),
        httpx.ConnectTimeout("no connection"),
        httpx.PoolTimeout("no connection"),
    ],
)
async def test_only_known_not_sent_failures_are_retried(make_app, response):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="7").artifact
    report, calls = await deliver(app, response)
    assert report.retrying == [artifact.outbox_id]
    assert app.outbox.get(artifact.outbox_id).status == "pending"
    assert len(calls) == 1  # the dispatcher schedules, the uploader does not retry in a loop
    if response == 429:
        assert app.outbox.get(artifact.outbox_id).not_before == app.clock.now() + 17


@pytest.mark.parametrize(
    "response",
    [
        500,
        502,
        httpx.ReadTimeout("lost reply"),
        httpx.WriteTimeout("partial upload"),
        httpx.RemoteProtocolError("lost connection"),
    ],
)
@pytest.mark.parametrize("interactive", [False, True])
async def test_ambiguous_upload_outcomes_are_parked_and_never_blindly_retried(
    make_app, response, interactive
):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="7", interactive=interactive).artifact
    report, calls = await deliver(app, response)
    assert report.unknown == [artifact.outbox_id]
    assert app.outbox.get(artifact.outbox_id).status == "maybe_sent"
    assert app.transcript.latest() == []
    assert len(calls) == 1
    app.clock.advance(600)
    again, calls = await deliver(app)
    assert not again.sent and not calls


async def test_expired_artifacts_are_not_uploaded(make_app):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="7").artifact
    app.clock.advance(31 * 86400)
    report, calls = await deliver(app)
    assert report.failed == [artifact.outbox_id] and not calls


async def test_a_file_cannot_be_sent_to_an_unpaired_chat(make_app):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="8").artifact
    report, calls = await deliver(app)
    assert report.failed == [artifact.outbox_id] and not calls


async def test_owner_requested_artifact_can_arrive_during_quiet_hours(make_app):
    app = make_app(config=Config(notify=NotifyConfig(quiet_hours="00:00-23:59")))
    source(app)
    artifact = publish(app, channel="telegram", recipient="7", interactive=True).artifact
    report, calls = await deliver(app)
    assert report.sent == [artifact.outbox_id] and len(calls) == 1


def test_interrupted_interactive_artifact_upload_is_not_replayed_after_crash(make_app):
    app = make_app()
    source(app)
    artifact = publish(app, channel="telegram", recipient="7", interactive=True).artifact
    assert len(app.outbox.claim_due()) == 1
    app.recover()
    assert app.outbox.get(artifact.outbox_id).status == "maybe_sent"


@pytest.mark.parametrize("change", ["revoke", "recipient"])
async def test_recipient_is_rechecked_after_snapshot_read(make_app, monkeypatch, change):
    app = make_app()
    source(app)
    app.pairing.allow("telegram", "7", "owner")
    bind_owner(app, "telegram", "7")
    artifact = publish(
        app, channel="telegram", recipient="7" if change == "revoke" else None
    ).artifact
    ready, release = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    read = app.artifacts.read

    def paused_read(identifier):
        result = read(identifier)
        loop.call_soon_threadsafe(ready.set)
        if not release.wait(3):
            raise AssertionError("the test did not release the snapshot reader")
        return result

    monkeypatch.setattr(app.artifacts, "read", paused_read)
    transport = DocumentApi()
    channel = TelegramChannel(app, api=transport.api)
    task = asyncio.create_task(Dispatcher(app, {"telegram": channel}).deliver_once())
    try:
        await asyncio.wait_for(ready.wait(), 3)
        if change == "revoke":
            app.pairing.revoke("telegram", "7")
            revoke_owner(app, "telegram", "7")
        else:
            app.clock.advance(1)
            app.pairing.allow("telegram", "8", "new recipient")
            bind_owner(app, "telegram", "8")
            app.pairing.touch("telegram", "8")
        release.set()
        report = await task
    finally:
        release.set()
        if not task.done():
            await task
        await transport.client.aclose()
    assert report.failed == [artifact.outbox_id]
    assert not transport.calls and not app.transcript.latest()


@pytest.mark.parametrize("payload", [123, [{"id": "0" * 32, "name": "forged.exe"}]])
async def test_history_rebuilds_artifacts_from_the_store_not_payload_metadata(make_app, payload):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    app.db.execute(
        "UPDATE outbox SET payload=? WHERE id=?",
        (dumps({"text": "File ready.", "artifacts": payload}), artifact.outbox_id),
    )
    report = await Dispatcher(app, {"web": WebChannel(app)}).deliver_once()
    assert report.sent == [artifact.outbox_id]
    [turn] = app.transcript.latest()
    assert turn.meta["artifacts"] == [artifact.to_json()]


async def test_a_forged_notification_cannot_borrow_another_artifacts_file_card(make_app):
    app = make_app()
    source(app)
    artifact = publish(app).artifact
    message = app.outbox.enqueue(
        session_key="main",
        channel="web",
        text="A separate notification.",
        kind="artifact",
        extra={"artifacts": [artifact.to_json()]},
    )
    await Dispatcher(app, {"web": WebChannel(app)}).deliver_once()
    [forged] = [turn for turn in app.transcript.latest() if turn.text == "A separate notification."]
    assert "artifacts" not in forged.meta
    assert app.outbox.get(message).status == "sent"
