"""The connections to the model and speech services are closed when the services stop.

An HTTP client keeps its connections open for reuse, and those belong to the event loop they were
made in. A process that ends a loop without closing them leaves sockets for the garbage collector
to find (which is what the suite's leak detection reported on the first run against the real API),
and a client used again from a later loop can fail with "event loop is closed".
"""

from __future__ import annotations

import logging

import pytest

from fake_openai import FakeOpenAI
from helpers import watching_for_leaks
from personal_agent.app import build_app
from personal_agent.channels.transcribe import OpenAITranscriber, TranscriptionError
from personal_agent.config import Config, MediaConfig, ModelConfig
from personal_agent.harness.models import OpenAIModels

KEY = "sk-" + "test-" + "not-a-real-key"


@pytest.fixture(autouse=True)
def _a_key_and_no_proxy(monkeypatch):
    """The fake server is on this machine: nothing may route the request anywhere else."""
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)


class Closing:
    """Stands in for the model factory or the speech client, and counts being closed."""

    def __init__(self, *, fails: bool = False) -> None:
        self.closed = 0
        self.fails = fails

    def __call__(self, name: str):
        raise AssertionError("nothing here asks for a model")

    async def transcribe(self, data: bytes, *, mime: str, name: str = "") -> str:
        raise AssertionError("nothing here asks for a transcript")

    async def aclose(self) -> None:
        self.closed += 1
        if self.fails:
            raise RuntimeError("the connection would not close")


# ---- the clients ----------------------------------------------------------------------------------
async def test_the_model_client_is_closed_and_the_next_use_opens_a_new_one():
    models = OpenAIModels(ModelConfig())
    first = models.client
    assert not first.is_closed()
    await models.aclose()
    assert first.is_closed()
    await models.aclose()  # harmless the second time, and when no client was ever made
    second = models.client
    assert second is not first and not second.is_closed()
    await models.aclose()


async def test_the_speech_client_is_closed_and_the_next_use_opens_a_new_one():
    speech = OpenAITranscriber(ModelConfig(), MediaConfig())
    await speech.aclose()  # before any client was made: nothing to do
    first = speech._client_or_raise()
    await speech.aclose()
    assert first.is_closed()
    assert speech._client_or_raise() is not first
    await speech.aclose()


# ---- stopping the services ---------------------------------------------------------------------------
async def test_stopping_the_services_closes_the_model_and_the_speech_clients(tmp_path):
    models, speech = Closing(), Closing()
    app = build_app(
        home=tmp_path / "home", model_factory=models, transcriber=speech, read_env=False
    )
    try:
        await app.stop_services()
        assert (models.closed, speech.closed) == (1, 1)
        await app.stop_services()  # again, as when a failed start stops them and so does the end
        assert (models.closed, speech.closed) == (2, 2)
    finally:
        app.close()


async def test_a_client_that_cannot_close_does_not_keep_the_others_open(tmp_path, caplog):
    models, speech = Closing(fails=True), Closing()
    app = build_app(
        home=tmp_path / "home", model_factory=models, transcriber=speech, read_env=False
    )
    try:
        with caplog.at_level(logging.WARNING, logger="personal_agent.app"):
            await app.stop_services()  # does not raise
        assert (models.closed, speech.closed) == (1, 1)
        assert "could not close Closing cleanly" in caplog.text
    finally:
        app.close()


async def test_a_factory_with_nothing_to_close_is_left_alone(tmp_path):
    app = build_app(home=tmp_path / "home", model_factory=lambda _name: None, read_env=False)
    try:
        await app.stop_services()
    finally:
        app.close()


# ---- the real thing: a request over a real connection, and nothing left open --------------------------
async def _one_request_through_an_app(tmp_path, url: str) -> None:
    app = build_app(
        home=tmp_path / "home",
        config=Config(model=ModelConfig(base_url=url)),
        read_env=False,
    )
    try:
        reply = await app.model_factory.client.responses.create(model="fake", input="hello")
        assert reply.status == "completed"
        await app.stop_services()
    finally:
        app.close()


async def test_a_request_over_a_real_connection_leaves_nothing_open(tmp_path):
    with FakeOpenAI() as fake, watching_for_leaks() as leaks:
        await _one_request_through_an_app(tmp_path, fake.url)
    assert leaks == []


async def _one_failed_transcription(url: str) -> None:
    speech = OpenAITranscriber(ModelConfig(base_url=url), MediaConfig())
    with pytest.raises(TranscriptionError):  # the fake server has no such route: a 404
        await speech.transcribe(b"not really audio", mime="audio/mpeg", name="clip.mp3")
    await speech.aclose()


async def test_a_speech_request_over_a_real_connection_leaves_nothing_open():
    with FakeOpenAI() as fake, watching_for_leaks() as leaks:
        await _one_failed_transcription(fake.url)
    assert leaks == []


async def test_without_closing_the_same_request_does_leave_a_connection_open(tmp_path):
    """The check above can fail: this is the same request with the closing left out."""

    async def forgetful() -> None:
        models = OpenAIModels(ModelConfig(base_url=fake.url))
        await models.client.responses.create(model="fake", input="hello")

    with FakeOpenAI() as fake, watching_for_leaks() as leaks:
        await forgetful()
    assert any("unclosed" in message for message in leaks)
