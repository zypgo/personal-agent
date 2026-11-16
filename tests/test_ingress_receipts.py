from __future__ import annotations

import asyncio

import pytest

from personal_agent.channels.ingress import Ingress, Upload
from personal_agent.config import Config, MediaConfig
from personal_agent.core.receipts import ReceiptStore
from test_ingress import FakeTranscriber

VOICE = Upload(b"audio" * 1600, mime="audio/ogg", name="voice.ogg", duration_s=60)


def test_engine_restart_parks_interrupted_calls_and_preserves_completed_words(make_app):
    app = make_app()
    receipts = ReceiptStore(app.db, app.clock)
    receipts.claim("web", "in-flight", "one", 240)
    receipts.claim("web", "paid", "two", 240)
    receipts.save("web", "paid", "ready", {"words": "already transcribed"})

    assert app.recover()["interrupted_transcriptions"] == 1
    assert receipts.claim("web", "in-flight", "one", 240).state == "uncertain"
    paid = receipts.claim("web", "paid", "two", 240)
    assert paid.state == "ready" and paid.result == {"words": "already transcribed"}
    assert app.recover()["interrupted_transcriptions"] == 0


async def test_repeated_voice_delivery_is_billed_and_transcribed_once(make_app):
    transcriber = FakeTranscriber("call mom")
    app = make_app(transcriber=transcriber)
    ingress = Ingress(app)
    first = await ingress.owner_message(channel="web", voice=VOICE, external_id="voice-1")
    cost = app.budget.spent_today()
    again = await Ingress(app).owner_message(channel="web", voice=VOICE, external_id="voice-1")
    assert (first.status, again.status) == ("queued", "duplicate")
    assert len(transcriber.calls) == 1 and app.budget.spent_today() == cost
    assert app.db.scalar("SELECT COUNT(*) FROM events") == 1


async def test_ready_transcription_survives_failure_before_event_insertion(make_app, monkeypatch):
    transcriber = FakeTranscriber("already paid words")
    app = make_app(transcriber=transcriber)
    ingress = Ingress(app)

    def crash(_images):
        raise RuntimeError("crash after transcription commit")

    monkeypatch.setattr(ingress, "_store_images", crash)
    with pytest.raises(RuntimeError, match="after transcription"):
        await ingress.owner_message(channel="web", voice=VOICE, external_id="voice-2")
    assert app.db.scalar("SELECT state FROM ingress_receipts") == "ready"
    cost = app.budget.spent_today()
    result = await Ingress(app).owner_message(channel="web", voice=VOICE, external_id="voice-2")
    assert result.status == "queued"
    assert len(transcriber.calls) == 1 and app.budget.spent_today() == cost


async def test_concurrent_duplicate_does_not_start_another_external_call(make_app):
    started, release = asyncio.Event(), asyncio.Event()

    class Slow:
        calls = 0

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            started.set()
            await release.wait()
            return "one call"

    transcriber = Slow()
    app = make_app(transcriber=transcriber)
    task = asyncio.create_task(
        Ingress(app).owner_message(channel="web", voice=VOICE, external_id="concurrent")
    )
    try:
        await started.wait()
        duplicate = await Ingress(app).owner_message(
            channel="web", voice=VOICE, external_id="concurrent"
        )
        assert duplicate.status == "rejected" and "still being transcribed" in duplicate.reply
        assert transcriber.calls == 1
    finally:
        release.set()
        assert (await task).status == "queued"


async def test_timed_out_transcription_is_not_automatically_rebilled(make_app):
    class Slow:
        calls = 0

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            await asyncio.sleep(5)
            return "late"

    transcriber = Slow()
    app = make_app(
        transcriber=transcriber, config=Config(media=MediaConfig(transcribe_timeout_s=0.01))
    )
    ingress = Ingress(app)
    first = await ingress.owner_message(channel="web", voice=VOICE, external_id="timeout")
    again = await ingress.owner_message(channel="web", voice=VOICE, external_id="timeout")
    assert "timed out" in first.reply and "interrupted" in again.reply
    assert transcriber.calls == 1


async def test_expired_inflight_receipt_requires_a_new_message(make_app):
    transcriber = FakeTranscriber()
    app = make_app(transcriber=transcriber)
    ingress = Ingress(app)

    # Simulate death after the durable claim but before its external call returns.
    class Crash(BaseException):
        pass

    async def disappear(*args, **kwargs):
        raise Crash()

    app.transcriber.transcribe = disappear
    with pytest.raises(Crash):
        await ingress.owner_message(channel="web", voice=VOICE, external_id="expired")
    app.clock.advance(300)
    app.transcriber = FakeTranscriber()
    result = await ingress.owner_message(channel="web", voice=VOICE, external_id="expired")
    assert result.status == "rejected" and "interrupted" in result.reply
    assert app.transcriber.calls == []


async def test_same_id_with_changed_voice_cannot_reuse_cached_words(make_app, monkeypatch):
    app = make_app(transcriber=FakeTranscriber())
    ingress = Ingress(app)

    def crash(_images):
        raise RuntimeError("after transcription")

    monkeypatch.setattr(ingress, "_store_images", crash)
    with pytest.raises(RuntimeError):
        await ingress.owner_message(channel="web", voice=VOICE, external_id="same-id")
    result = await Ingress(app).owner_message(
        channel="web", voice=Upload(b"different audio"), external_id="same-id"
    )
    assert result.status == "rejected" and "different content" in result.reply
