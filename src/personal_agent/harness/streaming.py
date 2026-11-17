"""Only public answer text is streamed; reasoning and tool arguments never reach the page."""

from __future__ import annotations

import asyncio

from agents import RunResultStreaming

from personal_agent.core.live import LiveBus


async def consume_stream(result: RunResultStreaming, live: LiveBus, run_id: str) -> None:
    """Consume the SDK completely before recording a final result or an approval checkpoint.

    Browser disconnects do not own this task. A cancelled worker, however, must settle the SDK
    task before its database and tools are closed. A failed streaming request is never silently
    replayed as a non-streaming request, since tools may already have changed the world.
    """
    live.response_started(run_id)
    try:
        async for event in result.stream_events():
            if event.type != "raw_response_event":
                continue
            data = event.data
            if data.type == "response.created":
                live.response_started(run_id)
            elif data.type == "response.output_text.delta":
                live.response_text(run_id, data.delta)
            elif data.type == "response.output_text.done":
                live.response_text(run_id, data.text, complete=True)
        if result.run_loop_exception is not None:
            raise result.run_loop_exception
    except BaseException as failure:
        result.cancel()
        try:
            # The SDK's cancellation path returns before provider/sandbox cleanup finishes.
            # Its public contract requires another drain after cancel(); awaiting only the
            # main task would miss cleanup tasks that start from that task's done callback.
            async for _ in result.stream_events():
                pass
        except asyncio.CancelledError:
            # A second cancellation must still reach the worker's shutdown/deadline handler.
            raise
        except BaseException:
            # The SDK can re-raise the original stream failure while draining. Preserve the
            # first failure even if a cleanup handler failed differently.
            raise failure from None
        raise
    finally:
        live.response_finished(run_id)
