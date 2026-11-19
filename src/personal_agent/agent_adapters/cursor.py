"""Cursor ACP provider using the official agent-client-protocol Python SDK."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Sequence
from typing import Any, cast

from acp import PROTOCOL_VERSION, connect_to_agent
from acp.interfaces import Client
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    DeniedOutcome,
    Implementation,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallUpdate,
)

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest, NativeResult
from personal_agent.agent_adapters.native_process import NativeProcess

MAX_TEXT = 240_000


class CursorReader(asyncio.StreamReader):
    """Normalize Cursor's documented unprefixed extensions for the SDK extension router.

    The SDK still owns framing, validation, request IDs, response routing and lifecycle.
    Its extension convention uses _cursor/...; Cursor documents cursor/... on the wire.
    """

    def __init__(self, reader: asyncio.StreamReader) -> None:
        super().__init__(limit=1_048_576)
        self.source = reader

    async def readuntil(self, separator: Any = b"\n") -> bytes:
        try:
            raw = await self.source.readuntil(separator)
        except asyncio.LimitOverrunError:
            raise BridgeError("Cursor protocol response exceeds its limit.") from None
        if len(raw) > 1_048_576:
            raise BridgeError("Cursor protocol response exceeds its limit.")
        try:
            message = json.loads(raw)
        except ValueError:
            return raw
        if isinstance(message, dict) and str(message.get("method", "")).startswith("cursor/"):
            message["method"] = "_" + message["method"]
            return json.dumps(message).encode() + b"\n"
        return raw


class CursorClient:
    def __init__(
        self, request: NativeRequest, callbacks: NativeCallbacks, cancel: asyncio.Event
    ) -> None:
        self.request, self.callbacks, self.cancel = request, callbacks, cancel
        self.chunks: list[str] = []
        self.text_size = 0

    def question_id(self, identity: str) -> str:
        return (
            "cursor_" + hashlib.sha256((self.request.run_id + identity).encode()).hexdigest()[:24]
        )

    async def request_permission(
        self,
        session_id: str,
        tool_call: ToolCallUpdate,
        options: list[PermissionOption],
        **kwargs: Any,
    ) -> RequestPermissionResponse:
        allowed = {
            option.option_id: option
            for option in options
            if option.kind in {"allow_once", "reject_once"}
        }
        details = tool_call.model_dump_json(by_alias=True)
        if self.cancel.is_set() or len(details) > 40_000 or not allowed:
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        description = (
            "Cursor native tool approval:\n"
            + details
            + "\n"
            + "\n".join(f"{key}: {value.name}" for key, value in allowed.items())
        )
        answer = await self.callbacks.question(
            self.question_id(tool_call.tool_call_id), description, list(allowed)
        )
        if answer not in allowed or self.cancel.is_set():
            return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        return RequestPermissionResponse(
            outcome=AllowedOutcome(outcome="selected", option_id=answer)
        )

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        value = update.model_dump(by_alias=True, exclude_none=True)
        kind = value.get("sessionUpdate")
        if kind == "agent_message_chunk" and value.get("content", {}).get("type") == "text":
            text = value["content"]["text"]
            self.text_size += len(text)
            if self.text_size > MAX_TEXT:
                self.cancel.set()
                return
            self.chunks.append(text)
        elif kind == "tool_call_update" and value.get("status") == "completed":
            await self.callbacks.progress("Cursor completed a native tool call.")
        elif kind == "usage_update":
            await self.callbacks.state({"usage": value, "cost_status": "unknown"})

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self.cancel.is_set():
            return {"outcome": {"outcome": "cancelled"}}
        question_id = self.question_id(str(params.get("toolCallId", method)))
        if method == "cursor/create_plan":
            text = json.dumps(params, ensure_ascii=False)
            if len(text) > 40_000:
                return {
                    "outcome": {
                        "outcome": "rejected",
                        "reason": "Plan exceeds the host review limit.",
                    }
                }
            answer = await self.callbacks.question(
                question_id, "Cursor plan approval:\n" + text, ["accept", "reject"]
            )
            outcome = "accepted" if answer == "accept" and not self.cancel.is_set() else "rejected"
            return {"outcome": {"outcome": outcome}}
        if method == "cursor/ask_question":
            answers = []
            for index, question in enumerate(params.get("questions", [])):
                options = {
                    str(option["id"]): str(option["label"])
                    for option in question.get("options", [])
                }
                text = (
                    str(question.get("prompt", ""))
                    + "\n"
                    + "\n".join(f"{key}: {label}" for key, label in options.items())
                )
                answer = await self.callbacks.question(
                    f"{question_id}_{index}", text, list(options)
                )
                if answer not in options or self.cancel.is_set():
                    return {"outcome": {"outcome": "cancelled"}}
                answers.append({"questionId": str(question["id"]), "selectedOptionIds": [answer]})
            return {"outcome": {"outcome": "answered", "answers": answers}}
        raise BridgeError("Unsupported Cursor extension request.")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        if method in {"cursor/update_todos", "cursor/task", "cursor/generate_image"}:
            await self.callbacks.progress(f"Cursor update: {method}.")


class CursorProvider:
    def __init__(self, command: Sequence[str] = ("agent", "acp")) -> None:
        self.command = tuple(command)

    async def run(
        self, request: NativeRequest, callbacks: NativeCallbacks, cancel: asyncio.Event
    ) -> NativeResult:
        native = await NativeProcess.start(self.command, request, callbacks)
        process = native.process
        if process.stdin is None or process.stdout is None:
            await native.close()
            return NativeResult("unknown", "Cursor did not provide a protocol channel.")
        client = CursorClient(request, callbacks, cancel)
        # Omitted optional Client methods are deliberately unadvertised and return method-not-found.
        connection = connect_to_agent(
            cast(Client, client), process.stdin, CursorReader(process.stdout)
        )
        prompt: asyncio.Task[Any] | None = None
        cancel_wait = asyncio.create_task(cancel.wait())
        session_id = ""
        result = NativeResult("unknown", "Cursor turn outcome is unconfirmed; do not restart it.")
        try:
            initialized = await asyncio.wait_for(
                connection.initialize(
                    protocol_version=PROTOCOL_VERSION,
                    client_capabilities=ClientCapabilities(),
                    client_info=Implementation(name="personal-agent-host", version="1"),
                ),
                timeout=30,
            )
            if initialized.protocol_version != PROTOCOL_VERSION:
                raise BridgeError("Cursor negotiated an unsupported protocol version.")
            # Existing CLI auth must be configured by the owner. Do not initiate login here.
            session = await asyncio.wait_for(
                connection.new_session(cwd=str(request.cwd), mcp_servers=[]), timeout=30
            )
            session_id = session.session_id
            await callbacks.state({"session_id": session_id})
            if cancel.is_set():
                result = NativeResult("cancelled", "Cancelled before starting a Cursor prompt.")
            else:
                await callbacks.state(
                    {"turn_id": request.run_id, "turn_id_kind": "local_correlation"}
                )
                prompt = asyncio.create_task(
                    connection.prompt(
                        session_id=session_id,
                        prompt=[TextContentBlock(type="text", text=request.prompt)],
                    )
                )
                done, _ = await asyncio.wait(
                    {prompt, cancel_wait}, return_when=asyncio.FIRST_COMPLETED
                )
                if cancel_wait in done and not prompt.done():
                    await asyncio.wait_for(connection.cancel(session_id=session_id), timeout=5)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(prompt), timeout=5)
                if prompt.done() and not prompt.cancelled():
                    response = prompt.result()
                    await callbacks.state({"native_terminal": response.stop_reason})
                    if response.stop_reason == "end_turn":
                        result = NativeResult(
                            "succeeded",
                            "".join(client.chunks) or "Cursor completed without a text result.",
                        )
                    elif response.stop_reason == "cancelled":
                        result = NativeResult("cancelled", "Cursor reported its prompt cancelled.")
                    else:
                        result = NativeResult(
                            "failed", "Cursor ended the prompt without a completed result."
                        )
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception:
            cancel.set()
        finally:
            if prompt is not None and not prompt.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(connection.cancel(session_id=session_id), timeout=2)
                prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
            await connection.close()
            stopped = await native.close()
        if not stopped:
            return NativeResult("unknown", "Cursor process group termination is unconfirmed.")
        return result
