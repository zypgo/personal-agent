"""A streamed preview must not expose bytes the eventual message would scrub."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import cast

import pytest
from agents import Agent, Runner, RunResultStreaming
from agents.models.multi_provider import MultiProvider
from agents.testing import ModelStep, ScriptedModel

from personal_agent.channels.web.api import sse
from personal_agent.core.live import MAX_DRAFT_CHARS, MAX_DRAFTS, LiveBus, preview_text
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


# ---- Gaps found by breaking the code on purpose (scripts/mutation/lists/v02_channels_files.py) ----


def test_a_preview_is_cut_at_the_size_limit_and_says_so():
    live = LiveBus()
    live.response_started("run")
    live.response_text("run", "x" * (MAX_DRAFT_CHARS + 50))
    [draft] = live.response_drafts()
    assert len(draft["text"]) == MAX_DRAFT_CHARS and draft["truncated"] is True
    live.response_text("run", "more words")
    assert len(live.response_drafts()[0]["text"]) == MAX_DRAFT_CHARS


def test_a_cut_off_preview_still_withholds_the_start_of_a_credential(monkeypatch):
    secret = "fictitiousReviewCredentialXYZ"
    monkeypatch.setenv("PA_REVIEW_KEY", secret)
    live = LiveBus()
    live.response_started("run")
    # A complete message that continues past the limit, which falls inside the credential:
    # what was cut off is not settled, so a possible beginning of the credential stays hidden.
    live.response_text("run", "a" * (MAX_DRAFT_CHARS - 9) + secret, complete=True)
    [draft] = live.response_drafts()
    assert draft["truncated"] is True
    assert draft["text"] == "a" * (MAX_DRAFT_CHARS - 9)


def test_only_a_few_runs_are_previewed_at_once():
    live = LiveBus()
    for number in range(MAX_DRAFTS + 3):
        live.response_started(f"run-{number}")
        live.response_text(f"run-{number}", "hello")
    assert [d["run_id"] for d in live.response_drafts()] == [f"run-{n}" for n in range(MAX_DRAFTS)]
    live.response_finished("run-0")
    live.response_started("run-late")
    assert "run-late" in {d["run_id"] for d in live.response_drafts()}


def test_text_for_a_run_nobody_started_is_ignored():
    live = LiveBus()
    live.response_text("ghost", "hello")
    assert live.response_drafts() == []


def test_a_new_model_attempt_starts_again_from_nothing():
    live = LiveBus()
    live.response_started("run")
    live.response_text("run", "first attempt")
    live.response_started("run")
    assert live.response_drafts()[0]["text"] == ""
    live.response_text("run", "second")
    assert live.response_drafts()[0]["text"] == "second"


def raw(kind, **fields):
    return SimpleNamespace(type="raw_response_event", data=SimpleNamespace(type=kind, **fields))


class ScriptedStream:
    """A public result stub: some events, then perhaps a failure the SDK recorded itself."""

    def __init__(self, events, live, failure=None):
        self.events, self.live, self.run_loop_exception = events, live, failure
        self.seen: list[list[str]] = []  # the previews as they stood after each event
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    async def stream_events(self) -> AsyncIterator[object]:
        if self.cancelled:
            return
        for event in self.events:
            yield event
            self.seen.append([d["text"] for d in self.live.response_drafts()])


async def test_only_answer_text_ever_reaches_the_preview():
    live = LiveBus()
    result = ScriptedStream(
        [
            raw("response.created"),
            raw("response.reasoning_summary_text.delta", delta="private reasoning "),
            raw("response.function_call_arguments.delta", delta='{"path": "secret.txt"}'),
            raw("response.output_text.delta", delta="Hello"),
            raw("response.output_text.delta", delta=" world"),
            raw("response.output_text.done", text="Hello world"),
        ],
        live,
    )
    await consume_stream(cast(RunResultStreaming, result), live, "run")
    assert result.seen[-1] == ["Hello world"]
    shown = " ".join(text for texts in result.seen for text in texts)
    assert "private" not in shown and "secret.txt" not in shown
    assert not live.response_drafts()


async def test_a_failure_the_sdk_recorded_after_the_stream_ended_is_raised():
    failure = RuntimeError("the model request failed")
    live = LiveBus()
    result = ScriptedStream([raw("response.output_text.delta", delta="partial")], live, failure)
    with pytest.raises(RuntimeError) as caught:
        await consume_stream(cast(RunResultStreaming, result), live, "run")
    assert caught.value is failure
    assert result.cancelled
    assert not live.response_drafts()
