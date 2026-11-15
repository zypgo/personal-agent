"""A stand-in for the Telegram Bot API, served through `httpx.MockTransport` (no network)."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

TOKEN = "123456789" + ":" + "A" * 35  # built in pieces so no scanner mistakes it for a real one


@dataclass
class Fail:
    status: int
    description: str
    retry_after: float | None = None


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.batches: list[list[dict[str, Any]]] = []
        self.files: dict[str, bytes] = {}
        self.file_sizes: dict[str, int] = {}
        self._scripts: dict[str, list[Any]] = defaultdict(list)
        self._message_ids = iter(range(100, 10_000))

    # ---- setting the scene --------------------------------------------------------------
    def script(self, method: str, *outcomes: Any) -> None:
        """What the next calls of `method` do, in order: a `Fail`, an exception to raise, or the
        result to return. After that the method behaves normally."""
        self._scripts[method].extend(outcomes)

    def deliver(self, *updates: dict[str, Any]) -> None:
        """Updates the next `getUpdates` call returns (one call per `deliver`)."""
        self.batches.append(list(updates))

    def file(self, file_id: str, data: bytes, *, claimed_size: int | None = None) -> str:
        self.files[file_id] = data
        if claimed_size is not None:
            self.file_sizes[file_id] = claimed_size
        return file_id

    # ---- what happened -----------------------------------------------------------------
    def count(self, method: str) -> int:
        return sum(1 for name, _ in self.calls if name == method)

    def params(self, method: str) -> list[dict[str, Any]]:
        return [p for name, p in self.calls if name == method]

    def sent(self) -> list[dict[str, Any]]:
        return self.params("sendMessage")

    def texts(self) -> list[str]:
        return [p["text"] for p in self.sent()]

    # ---- the wire ------------------------------------------------------------------------
    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/file/bot{TOKEN}/"):
            file_id = path.rsplit("/", 1)[1]
            if file_id not in self.files:
                return httpx.Response(404)
            return httpx.Response(200, content=self.files[file_id])
        assert path.startswith(f"/bot{TOKEN}/"), path
        method = path.rsplit("/", 1)[1]
        params = json.loads(request.content or b"{}")
        self.calls.append((method, params))
        queue = self._scripts[method]
        outcome = queue.pop(0) if queue else self._default(method, params)
        if method == "getUpdates" and outcome == []:
            await asyncio.sleep(0.02)  # a real long poll waits; a hot loop would starve the others
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, Fail):
            body: dict[str, Any] = {
                "ok": False,
                "error_code": outcome.status,
                "description": outcome.description,
            }
            if outcome.retry_after is not None:
                body["parameters"] = {"retry_after": outcome.retry_after}
            return httpx.Response(outcome.status, json=body)
        return httpx.Response(200, json={"ok": True, "result": outcome})

    def _default(self, method: str, params: dict[str, Any]) -> Any:
        if method == "getMe":
            return {"id": 1, "is_bot": True, "username": "test_bot"}
        if method == "getUpdates":
            return self.batches.pop(0) if self.batches else []
        if method == "sendMessage":
            return {"message_id": next(self._message_ids), "chat": {"id": params["chat_id"]}}
        if method == "getFile":
            file_id = params["file_id"]
            if file_id not in self.files:
                return Fail(400, "Bad Request: invalid file_id")
            return {
                "file_id": file_id,
                "file_path": f"files/{file_id}",
                "file_size": self.file_sizes.get(file_id, len(self.files[file_id])),
            }
        return True


class FakeDaemon:
    """Just what a channel asks of the daemon."""

    def __init__(self, stop_when: Callable[[], bool] | None = None) -> None:
        self.stop_when = stop_when
        self.woken = 0
        self._stop = False

    @property
    def stopping(self) -> bool:
        return self._stop or bool(self.stop_when and self.stop_when())

    def stop(self) -> None:
        self._stop = True

    def wake(self) -> None:
        self.woken += 1


# ---- updates as Telegram sends them --------------------------------------------------------
def user(user_id: int = 7, *, first_name: str = "Owner", is_bot: bool = False) -> dict[str, Any]:
    return {"id": user_id, "is_bot": is_bot, "first_name": first_name}


def message(
    update_id: int,
    *,
    sender: int = 7,
    chat_type: str = "private",
    chat_id: int | None = None,
    first_name: str = "Owner",
    is_bot: bool = False,
    **content: Any,
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 10,
            "from": user(sender, first_name=first_name, is_bot=is_bot),
            "chat": {"id": sender if chat_id is None else chat_id, "type": chat_type},
            "date": 1_700_000_000,
            **content,
        },
    }


def text(update_id: int, body: str, **kw: Any) -> dict[str, Any]:
    return message(update_id, text=body, **kw)


def callback(
    update_id: int, data: str, *, sender: int = 7, message_id: int = 50, chat_id: int | None = None
) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb{update_id}",
            "from": user(sender),
            "data": data,
            "message": {
                "message_id": message_id,
                "chat": {"id": sender if chat_id is None else chat_id, "type": "private"},
            },
        },
    }
