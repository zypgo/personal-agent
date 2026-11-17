"""A streamed preview must not expose bytes the eventual message would scrub."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from agents import Agent, Runner, RunResultStreaming
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel

from personal_agent.channels.web.api import sse
from personal_agent.core.live import LiveBus, preview_text
from personal_agent.harness.streaming import consume_stream


@pytest.mark.parametrize("hidden", ["\u202e", "\x1b", "\u2066"])
def test_normalization_cannot_expose_a_split_known_credential(monkeypatch, hidden):
    secret = "fictitiousReviewCredentialXYZ"
    monkeypatch.setenv("PA_REVIEW_KEY", secret)
    disguised = secret[:9] + hidden + secret[9:]
    for size in range(1, len(disguised)):
        assert preview_text("Answer: " + disguised[:size]) == "Answer: "
    assert preview_text("Answer: " + disguised) == "Answer: [REDACTED]"
    assert preview_text("Answer: " + disguised, settled=True) == "Answer: [REDACTED]"


@pytest.mark.parametrize("separator", ["\r\n", "\u202e"])
def test_known_multiline_or_hidden_credentials_use_the_same_normalization(monkeypatch, separator):
    secret = "fictitious" + separator + "ReviewCredentialXYZ"
    monkeypatch.setenv("PA_REVIEW_KEY", secret)
    assert preview_text("Answer: " + secret[:15]) == "Answer: "
    assert preview_text("Answer: " + secret, settled=True) == "Answer: [REDACTED]"


@pytest.mark.parametrize("complete", [False, True])
def test_a_malformed_provider_string_cannot_break_the_sse_connection(complete):
    live = LiveBus()
    live.response_started("run")
    live.response_text("run", "hello \ud800 world \udfff", complete=complete)
    event = sse("drafts", {"drafts": live.response_drafts()}).decode("utf-8")
    assert "hello \ufffd world \ufffd" in event


@pytest.fixture
async def cancellable_sdk_stream(monkeypatch):
    """Use the actual SDK's cancellation and owned-provider cleanup, with no HTTP transport."""
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_finished = asyncio.Event()
    close = MultiProvider.aclose

    async def slow_close(provider):
        cleanup_started.set()
        await release_cleanup.wait()
        await close(provider)
        cleanup_finished.set()

    async def blocked_response(_call):
        started.set()
        await asyncio.Future()
        return []

    monkeypatch.setattr(MultiProvider, "aclose", slow_close)
    agent = Agent(
        name="offline streaming test", model=ScriptedModel([ModelStep.respond(blocked_response)])
    )
    # A config mapping lets Runner own its default provider; passing a RunConfig instance
    # explicitly transfers that provider's lifecycle to the caller in SDK 0.24.
    result = Runner.run_streamed(agent, "test", run_config={"tracing_disabled": True})
    live = LiveBus()
    task = asyncio.create_task(consume_stream(result, live, "run"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        yield task, live, cleanup_started, release_cleanup, cleanup_finished
    finally:
        release_cleanup.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.wait_for(cleanup_finished.wait(), timeout=5)


async def test_cancellation_waits_for_sdk_provider_cleanup(cancellable_sdk_stream):
    task, live, started, release, finished = cancellable_sdk_stream
    task.cancel("worker stopped")
    await asyncio.wait_for(started.wait(), timeout=5)
    # The main SDK task has already stopped; provider cleanup deliberately has not.
    done, _ = await asyncio.wait({task}, timeout=0.01)
    assert not done
    release.set()
    with pytest.raises(asyncio.CancelledError, match="worker stopped"):
        await task
    assert finished.is_set()
    assert not live.response_drafts()


async def test_a_second_cancellation_is_not_masked_by_stream_cleanup(cancellable_sdk_stream):
    task, live, started, _release, _finished = cancellable_sdk_stream
    task.cancel("first cancellation")
    await asyncio.wait_for(started.wait(), timeout=5)
    # Let consume_stream enter the public cleanup drain before cancelling that drain.
    done, _ = await asyncio.wait({task}, timeout=0.01)
    assert not done
    task.cancel("second cancellation")
    with pytest.raises(asyncio.CancelledError, match="second cancellation"):
        await task
    assert not live.response_drafts()


async def test_a_cleanup_error_does_not_replace_the_stream_failure():
    failure = RuntimeError("original provider failure")

    class FailingStream:
        """A public result stub whose cleanup also fails, after the original stream failure."""

        cancelled = False

        def cancel(self):
            self.cancelled = True

        async def stream_events(self) -> AsyncIterator[object]:
            if self.cancelled:
                raise ValueError("secondary cleanup failure")
            # One harmless event makes this an async generator, like the SDK's public method.
            yield type("IgnoredEvent", (), {"type": "agent_updated_stream_event"})()
            raise failure

    live = LiveBus()
    result = FailingStream()
    with pytest.raises(RuntimeError) as caught:
        await consume_stream(cast(RunResultStreaming, result), live, "run")
    assert caught.value is failure
    assert result.cancelled
    assert not live.response_drafts()
