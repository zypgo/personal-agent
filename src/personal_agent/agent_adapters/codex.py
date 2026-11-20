"""Codex app-server stdio adapter. Request shapes follow the CLI-generated schemas.

No model API fallback, blind resume, shell command construction, or approval bypass.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from personal_agent.agent_adapters.bridge import BridgeError
from personal_agent.agent_adapters.host_protocol import NativeCallbacks, NativeRequest, NativeResult
from personal_agent.agent_adapters.native_process import NativeProcess

MAX_TEXT = 240_000


class BeforeTurnCancelled(Exception):
    pass


class CodexRPC:
    """Codex's JSON-RPC-style NDJSON transport intentionally omits the jsonrpc field."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        notification: Callable[[str, dict[str, Any]], Awaitable[None]],
        request: Callable[[str, dict[str, Any], str], Awaitable[dict[str, Any]]],
    ) -> None:
        self.reader, self.writer = reader, writer
        self.notification, self.server_request = notification, request
        self.pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self.handlers: set[asyncio.Task[None]] = set()
        self.counter = 0
        self.failed = False
        self.write_lock = asyncio.Lock()
        self.task = asyncio.create_task(self._read())

    async def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, allow_nan=False).encode() + b"\n"
        if len(encoded) > 1_048_576:
            raise BridgeError("Native protocol request exceeds its limit.")
        async with self.write_lock:
            self.writer.write(encoded)
            await self.writer.drain()

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.counter += 1
        identity = self.counter
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        try:
            await self.send({"id": identity, "method": method, "params": params})
            return await asyncio.wait_for(future, 30)
        finally:
            self.pending.pop(identity, None)

    async def _answer(self, message: dict[str, Any]) -> None:
        try:
            result = await self.server_request(
                message["method"], message.get("params", {}), str(message["id"])
            )
            await self.send({"id": message["id"], "result": result})
        except asyncio.CancelledError:
            raise
        except Exception:
            with contextlib.suppress(OSError, RuntimeError):
                await self.send(
                    {
                        "id": message["id"],
                        "error": {
                            "code": -32601,
                            "message": "Request cannot be approved by this host.",
                        },
                    }
                )

    async def _read(self) -> None:
        try:
            while line := await self.reader.readline():
                if len(line) > 1_048_576:
                    raise BridgeError("Native protocol response exceeds its limit.")
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise BridgeError("Invalid native protocol response.")
                if "method" in message:
                    if "id" in message:
                        handler = asyncio.create_task(self._answer(message))
                        self.handlers.add(handler)
                        handler.add_done_callback(self.handlers.discard)
                    else:
                        await self.notification(message["method"], message.get("params", {}))
                else:
                    response_id = message.get("id")
                    future = self.pending.get(response_id) if type(response_id) is int else None
                    if future is not None and not future.done():
                        if "error" in message or not isinstance(message.get("result"), dict):
                            future.set_exception(
                                BridgeError("Native agent rejected a protocol request.")
                            )
                        else:
                            future.set_result(message["result"])
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failed = True
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(BridgeError("Native protocol connection ended."))

    async def close(self) -> None:
        self.task.cancel()
        for handler in tuple(self.handlers):
            handler.cancel()
        await asyncio.gather(self.task, *self.handlers, return_exceptions=True)


class CodexProvider:
    def __init__(
        self, command: Sequence[str] = ("codex", "app-server", "--listen", "stdio://")
    ) -> None:
        self.command = tuple(command)

    async def run(
        self, request: NativeRequest, callbacks: NativeCallbacks, cancel: asyncio.Event
    ) -> NativeResult:
        native = await NativeProcess.start(self.command, request, callbacks)
        process = native.process
        if process.stdout is None or process.stdin is None:
            await native.close()
            return NativeResult("unknown", "Native process did not provide a protocol channel.")
        session_id = ""
        turn_id = ""
        chunks: list[str] = []
        final_messages: dict[str, str] = {}
        text_size = 0
        completed: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()

        async def notification(method: str, params: dict[str, Any]) -> None:
            nonlocal text_size, turn_id
            if params.get("threadId") not in {None, session_id}:
                return
            if method == "turn/started":
                turn_id = str(params.get("turn", {}).get("id", turn_id))
                await callbacks.state({"turn_id": turn_id})
            elif method == "item/agentMessage/delta":
                delta = params.get("delta", "")
                if isinstance(delta, str):
                    text_size += len(delta)
                    if text_size > MAX_TEXT:
                        cancel.set()
                        return
                    chunks.append(delta)
            elif method == "item/completed":
                item = params.get("item", {})
                if item.get("type") == "agentMessage" and isinstance(item.get("text"), str):
                    final_messages[str(item.get("id", len(final_messages)))] = item["text"][
                        :MAX_TEXT
                    ]
                    if sum(map(len, final_messages.values())) > MAX_TEXT:
                        cancel.set()
                elif item.get("type") in {"commandExecution", "fileChange"}:
                    await callbacks.progress(f"Codex finished {item['type']} item.")
            elif method == "thread/tokenUsage/updated":
                await callbacks.state(
                    {"usage": params.get("tokenUsage", {}), "cost_status": "unknown"}
                )
            elif method == "turn/completed":
                turn = params.get("turn", {})
                if (not turn_id or turn.get("id") == turn_id) and not completed.done():
                    completed.set_result(turn)

        async def server_request(
            method: str, params: dict[str, Any], identity: str
        ) -> dict[str, Any]:
            question_id = (
                "codex_" + hashlib.sha256((request.run_id + identity).encode()).hexdigest()[:24]
            )
            if method in {
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            }:
                if cancel.is_set():
                    return {"decision": "cancel"}
                details = json.dumps(params, ensure_ascii=False)
                if len(details) > 40_000:
                    return {"decision": "decline"}
                decision = await callbacks.question(
                    question_id,
                    f"Codex native approval ({method}):\n{details}",
                    ["accept", "decline", "cancel"],
                )
                return {
                    "decision": decision
                    if decision in {"accept", "decline", "cancel"} and not cancel.is_set()
                    else "cancel"
                }
            if method == "item/tool/requestUserInput":
                answers: dict[str, Any] = {}
                for index, item in enumerate(params.get("questions", [])):
                    if item.get("isSecret"):
                        cancel.set()
                        return {"answers": {}}
                    options = [str(option.get("label", "")) for option in item.get("options", [])]
                    answer = await callbacks.question(
                        f"{question_id}_{index}", str(item.get("question", "")), options
                    )
                    if answer is None or cancel.is_set():
                        return {"answers": {}}
                    answers[str(item["id"])] = {"answers": [answer]}
                return {"answers": answers}
            raise BridgeError("Unsupported native approval or tool request.")

        rpc = CodexRPC(process.stdout, process.stdin, notification, server_request)
        cancel_wait = asyncio.create_task(cancel.wait())
        result = NativeResult("unknown", "Native turn outcome is unconfirmed; do not restart it.")
        try:
            await rpc.call(
                "initialize", {"clientInfo": {"name": "personal-agent-host", "version": "1"}}
            )
            await rpc.send({"method": "initialized"})
            if cancel.is_set():
                raise BeforeTurnCancelled
            started = await rpc.call(
                "thread/start",
                {
                    "cwd": str(request.cwd),
                    "approvalPolicy": "untrusted",
                    "approvalsReviewer": "user",
                    "sandbox": "workspace-write",
                },
            )
            session_id = str(started["thread"]["id"])
            await callbacks.state({"session_id": session_id})
            if cancel.is_set():
                raise BeforeTurnCancelled
            started_turn = await rpc.call(
                "turn/start",
                {
                    "threadId": session_id,
                    "input": [{"type": "text", "text": request.prompt}],
                    "clientUserMessageId": request.run_id,
                },
            )
            turn_id = str(started_turn["turn"]["id"])
            await callbacks.state({"turn_id": turn_id})
            waiters: set[asyncio.Future[Any]] = {completed, cancel_wait, rpc.task}
            done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            if cancel_wait in done and not completed.done():
                with contextlib.suppress(BridgeError, TimeoutError, OSError):
                    await asyncio.wait_for(
                        rpc.call("turn/interrupt", {"threadId": session_id, "turnId": turn_id}),
                        timeout=5,
                    )
                    await asyncio.wait_for(asyncio.shield(completed), timeout=5)
            if completed.done() and not completed.cancelled():
                turn = completed.result()
                text = "\n\n".join(final_messages.values()) or "".join(chunks)
                status = turn.get("status")
                if status == "completed":
                    result = NativeResult(
                        "succeeded", text or "Native turn completed without a text result."
                    )
                elif status == "interrupted":
                    result = NativeResult("cancelled", text or "Native turn reported interrupted.")
                elif status == "failed":
                    result = NativeResult(
                        "failed",
                        "Native turn reported failure. Inspect its native session for details.",
                    )
                await callbacks.state({"native_terminal": status})
        except BeforeTurnCancelled:
            result = NativeResult("cancelled", "Cancelled before starting a native turn.")
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception:
            cancel.set()
        finally:
            if turn_id and not completed.done():
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(
                        rpc.call("turn/interrupt", {"threadId": session_id, "turnId": turn_id}),
                        timeout=2,
                    )
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
            await rpc.close()
            stopped = await native.close()
        if not stopped:
            return NativeResult("unknown", "Native process group termination is unconfirmed.")
        return result
