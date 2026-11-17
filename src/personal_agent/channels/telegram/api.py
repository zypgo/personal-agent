"""A small Telegram Bot API client over httpx.

Only what the channel needs. The point of writing it by hand instead of pulling in a bot
framework is the error model: the dispatcher must know whether a message was *definitely not*
delivered (it may be sent again) or *might have been* (it must not be sent blindly again), and a
framework hides exactly that distinction.

- `NotSent`: the request never reached Telegram, or Telegram refused it before doing anything
  (connection refused, bad request, rate limit, blocked bot).
- any other `httpx` failure after the request was written (a read timeout, a dropped
  connection) is left to propagate unchanged: the outcome is unknown.

The token is part of every URL. Nothing here logs a URL, and `redact()` knows the token shape.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 10.0
CALL_TIMEOUT_S = 30.0
MAX_DOWNLOAD_BYTES = 20_000_000
"""Telegram's own limit for what a bot may download."""


class TelegramApiError(Exception):
    """Telegram answered, and the answer was an error."""

    def __init__(
        self,
        method: str,
        status: int,
        description: str,
        *,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"Telegram {method}: {status} {description}")
        self.method = method
        self.status = status
        self.description = description
        self.retry_after = retry_after

    @property
    def permanent(self) -> bool:
        """Retrying the same call cannot help: a wrong token, a user who blocked the bot."""
        return self.status in (400, 401, 403, 404)

    @property
    def unparsable(self) -> bool:
        """The text used markup Telegram could not parse (send it again as plain text)."""
        return self.status == 400 and "parse entities" in self.description.lower()


class NotSent(Exception):
    """The request never left this machine (no connection), so nothing was delivered."""


def _client(proxy: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
        # No redirects: Telegram never redirects, and a redirect would carry the token elsewhere.
        follow_redirects=False,
        headers={"User-Agent": "personal-agent"},
    )


def _result(method: str, response: httpx.Response) -> Any:
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code == 200 and body.get("ok"):
        return body.get("result")
    parameters = body.get("parameters")
    retry_after = parameters.get("retry_after") if isinstance(parameters, dict) else None
    raise TelegramApiError(
        method,
        response.status_code,
        str(body.get("description") or response.reason_phrase or "error"),
        retry_after=float(retry_after) if retry_after is not None else None,
    )


class TelegramApi:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        proxy: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base = f"{api_base.rstrip('/')}/bot{token}"
        self._file_base = f"{api_base.rstrip('/')}/file/bot{token}"
        self._client = client or _client(proxy)

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---- the generic call -------------------------------------------------------------
    async def call(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> Any:
        try:
            response = await self._client.post(
                f"{self._base}/{method}",
                json=params or {},
                timeout=timeout_s if timeout_s is not None else CALL_TIMEOUT_S,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            # Failed before a request was written. The message of the exception can hold the URL
            # (and so the token); do not chain it.
            raise NotSent(f"could not connect to Telegram ({type(exc).__name__})") from None
        return _result(method, response)

    # ---- what the channel uses --------------------------------------------------------
    async def get_me(self) -> dict[str, Any]:
        me: dict[str, Any] = await self.call("getMe")
        return me

    async def get_updates(self, offset: int, poll_s: int) -> list[dict[str, Any]]:
        updates: list[dict[str, Any]] = await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": poll_s,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout_s=poll_s + 15,
        )
        return updates

    async def delete_webhook(self) -> None:
        await self.call("deleteWebhook", {"drop_pending_updates": False})

    async def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        parse_mode: str | None = None,
        reply_markup: dict[str, Any] | None = None,
        silent: bool = False,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        if parse_mode:
            params["parse_mode"] = parse_mode
        if reply_markup:
            params["reply_markup"] = reply_markup
        if silent:
            params["disable_notification"] = True
        sent: dict[str, Any] = await self.call("sendMessage", params)
        return sent

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        await self.call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout_s=10)

    async def send_document(
        self,
        chat_id: str | int,
        content: bytes,
        *,
        filename: str,
        mime: str,
        caption: str = "",
        silent: bool = False,
    ) -> dict[str, Any]:
        """One multipart upload; late transport failures remain unknown, never auto-retried."""
        params = {"chat_id": str(chat_id), "caption": caption}
        if silent:
            params["disable_notification"] = "true"
        try:
            response = await self._client.post(
                f"{self._base}/sendDocument",
                data=params,
                files={"document": (filename, content, mime)},
                timeout=CALL_TIMEOUT_S,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise NotSent(f"could not connect to Telegram ({type(exc).__name__})") from None
        sent: dict[str, Any] = _result("sendDocument", response)
        return sent

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self.call(
            "answerCallbackQuery",
            {"callback_query_id": callback_id, "text": text[:190]},
            timeout_s=10,
        )

    async def clear_buttons(self, chat_id: str | int, message_id: int) -> None:
        await self.call(
            "editMessageReplyMarkup",
            {"chat_id": chat_id, "message_id": message_id, "reply_markup": {"inline_keyboard": []}},
            timeout_s=10,
        )

    async def download(self, file_id: str, *, max_bytes: int = MAX_DOWNLOAD_BYTES) -> bytes:
        """The bytes of a file the user sent. Refuses anything larger than `max_bytes`."""
        info = await self.call("getFile", {"file_id": file_id}, timeout_s=15)
        path = str(info.get("file_path") or "")
        if not path or path.startswith("/") or ".." in path.split("/"):
            raise TelegramApiError("getFile", 404, "no file path")
        size = info.get("file_size")
        if isinstance(size, int) and size > max_bytes:
            raise TelegramApiError("getFile", 413, "file too large")
        chunks: list[bytes] = []
        total = 0
        try:
            async with self._client.stream("GET", f"{self._file_base}/{path}") as response:
                if response.status_code != 200:
                    raise TelegramApiError("download", response.status_code, "download failed")
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise TelegramApiError("download", 413, "file too large")
                    chunks.append(chunk)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise NotSent(f"could not connect to Telegram ({type(exc).__name__})") from None
        return b"".join(chunks)
