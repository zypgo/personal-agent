"""Private application-robot conversations over DingTalk Stream, without a public listener.

Only locally paired staff IDs in the robot's own organization reach Ingress. Callback ACKs are
sent after local acceptance; the platform does not promise replay of missed robot messages.
Outbound replies and proactive notifications share one OpenAPI path and the normal Outbox.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any, Protocol

from websockets.asyncio.client import connect

from personal_agent.channels.base import ChannelCapabilities, ChannelError
from personal_agent.channels.dingtalk.api import (
    MESSAGE_TOPIC,
    DeliveryUncertain,
    DingTalkApi,
    DingTalkApiError,
)
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.pairing import PAIRING_TTL_S
from personal_agent.core.outbox import OutMessage
from personal_agent.core.sessions import DEFAULT_SESSION

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Daemon

log = logging.getLogger(__name__)
# WebSocket debug logging includes ticket URLs and raw callback payloads. Keep transport logs
# off even if the owner enables application debug logging; our diagnostics name only outcomes.
_TRANSPORT_LOG = logging.Logger("personal_agent.dingtalk.transport", logging.CRITICAL + 1)
MAX_FRAME_BYTES = 131_072
MESSAGE_CHUNK_BYTES = 3_500


class StreamSocket(Protocol):
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...

    async def send(self, message: str) -> None: ...


class _NoRedirectConnect(connect):
    # The URL contains a bearer ticket. Only the endpoint returned by the authenticated
    # gateway API is trusted, even if a redirect retains that ticket in its Location URL.
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def open_stream(url: str) -> AbstractAsyncContextManager[StreamSocket]:
    return _NoRedirectConnect(
        url,
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=MAX_FRAME_BYTES,
        max_queue=16,
        logger=_TRANSPORT_LOG,
    )


def _identifier(value: object) -> str | None:
    if (
        isinstance(value, str)
        and value
        and len(value) <= 512
        and all(c.isprintable() and not c.isspace() for c in value)
    ):
        return value
    return None


def recipient_key(client_id: str, corp_id: str, staff_id: str) -> str:
    """Scope approvals to both application and organization, even when staff IDs are reused."""
    return json.dumps([client_id, corp_id, staff_id], ensure_ascii=False, separators=(",", ":"))


def message_key(client_id: str, corp_id: str, message_id: str) -> str:
    value = json.dumps([client_id, corp_id, message_id], ensure_ascii=False, separators=(",", ":"))
    return "dd:" + hashlib.sha256(value.encode()).hexdigest()


def chunks(text: str, *, max_bytes: int = MESSAGE_CHUNK_BYTES) -> list[str]:
    """Bound UTF-8 payloads without dropping characters or splitting a Unicode code point."""
    parts: list[str] = []
    current: list[str] = []
    size = 0
    for char in text:
        width = len(char.encode("utf-8"))
        if current and size + width > max_bytes:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts or [""]


class DingTalkChannel:
    name = "dingtalk"
    capabilities = ChannelCapabilities(markdown=True, proactive=True)

    def __init__(
        self,
        app: App,
        *,
        api: DingTalkApi | None = None,
        connect_stream: Callable[[str], AbstractAsyncContextManager[StreamSocket]] = open_stream,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.app = app
        self.cfg = app.config.dingtalk
        self.client_id = os.environ.get(self.cfg.client_id_env, "").strip()
        self._secret = os.environ.get(self.cfg.client_secret_env, "").strip()
        if api is not None:
            self.client_id = api.client_id
        self.robot_code = self.cfg.robot_code or self.client_id
        self._api, self._owns_api = api, api is None
        self._connect, self._sleep = connect_stream, sleep
        self.state = "stopped"
        self.last_error: str | None = None
        self.last_connected_at: float | None = None
        self.last_received_at: float | None = None

    @property
    def configured(self) -> bool:
        return self._api is not None or bool(self.client_id and self._secret)

    @property
    def api(self) -> DingTalkApi:
        if self._api is None:
            if not self.configured:
                raise ChannelError(
                    f"Set {self.cfg.client_id_env} and {self.cfg.client_secret_env} to connect DingTalk",
                    permanent=True,
                )
            self._api = DingTalkApi(self.client_id, self._secret)
        return self._api

    def health(self) -> dict[str, Any]:
        """Observed in-process state, without tokens, request URLs, or message content."""
        return {
            "state": self.state,
            "configured": self.configured,
            "last_error": self.last_error,
            "last_connected_at": self.last_connected_at,
            "last_received_at": self.last_received_at,
        }

    async def close(self) -> None:
        if self._api is not None and self._owns_api:
            api, self._api = self._api, None
            await api.aclose()

    def _staff_for(self, message: OutMessage) -> str:
        target = message.recipient
        if not target:
            recent = self.app.pairing.recent_sender(self.name)
            if recent is None:
                raise ChannelError(
                    "No DingTalk account is paired. Message the robot, then run "
                    "`pa channels approve dingtalk CODE` on this computer.",
                    permanent=True,
                )
            target = recent.sender
        if not self.app.pairing.is_approved(self.name, target):
            raise ChannelError("That DingTalk account is not approved", permanent=True)
        try:
            parts = json.loads(target)
        except (TypeError, ValueError):
            parts = None
        if (
            not isinstance(parts, list)
            or len(parts) != 3
            or not all(_identifier(part) for part in parts)
            or parts[0] != self.client_id
            or (self.cfg.corp_id is not None and parts[1] != self.cfg.corp_id)
            or recipient_key(*parts) != target
        ):
            raise ChannelError(
                "That DingTalk pairing belongs to a different application or organization",
                permanent=True,
            )
        return str(parts[2])

    async def send(self, message: OutMessage) -> None:
        staff = self._staff_for(message)
        text = message.text
        if message.kind == "approval_request":
            ids = message.payload.get("approval_ids") or []
            ids = [i for i in ids if isinstance(i, int) and not isinstance(i, bool) and i > 0]
            if ids:
                text += f"\n\nReply `/approve {ids[0]}` or `/deny {ids[0]}`. `/approvals` lists all pending actions."
        for index, piece in enumerate(chunks(text)):
            try:
                # Recheck revocation between chunks, not just once before a long response.
                if self._staff_for(message) != staff:
                    raise ChannelError("The DingTalk recipient changed", permanent=True)
                await self.api.send_message(self.robot_code, staff, piece)
            except ChannelError:
                if index:
                    raise DeliveryUncertain(
                        f"{index} DingTalk message part(s) were accepted before delivery stopped"
                    ) from None
                raise

    async def run(self, daemon: Daemon) -> None:
        if not self.configured:
            self.state, self.last_error = (
                "error",
                f"Set {self.cfg.client_id_env} and {self.cfg.client_secret_env}",
            )
            log.error("DingTalk is enabled but credentials are missing")
            return
        ingress = Ingress(self.app, wake=daemon.wake)
        backoff = 1.0
        try:
            while not bool(daemon.stopping):
                self.state = "connecting"
                try:
                    url = await self.api.open_connection()  # A new one-use ticket every attempt.
                    async with self._connect(url) as socket:
                        self.state, self.last_error = "connected", None
                        self.last_connected_at = self.app.clock.now()
                        log.info("DingTalk Stream connected")
                        async for raw in socket:
                            backoff = 1.0
                            if not await self._frame(raw, socket, ingress) or daemon.stopping:
                                break
                except asyncio.CancelledError:
                    raise
                except DingTalkApiError as exc:
                    self.last_error = str(exc)
                    if exc.permanent:
                        self.state = "error"
                        log.error("DingTalk could not authenticate or open its Stream connection")
                        return
                except Exception as exc:
                    # Exceptions from a websocket can contain its ticket URL; never log them.
                    self.last_error = f"Stream interrupted ({type(exc).__name__})"
                if daemon.stopping:
                    break
                self.state = "reconnecting"
                log.info("DingTalk Stream disconnected; reconnecting")
                await self._sleep(backoff + random.uniform(0, 0.5))  # noqa: S311 - retry jitter
                backoff = min(backoff * 2, 60.0)
        finally:
            if self.state != "error":
                self.state = "stopped"
            await self.close()

    async def _frame(self, raw: str | bytes, socket: StreamSocket, ingress: Ingress) -> bool:
        try:
            frame = json.loads(raw)
        except (ValueError, TypeError):
            return True
        if not isinstance(frame, dict) or not isinstance(frame.get("headers"), dict):
            return True
        headers = frame["headers"]
        kind, topic = frame.get("type"), headers.get("topic")
        if kind == "SYSTEM" and topic == "disconnect":
            return False
        message_id = _identifier(headers.get("messageId"))
        if message_id is None:
            return True
        try:
            data = json.loads(frame.get("data", ""))
        except (ValueError, TypeError):
            await self._ack(socket, message_id, 400)
            return True
        if not isinstance(data, dict):
            await self._ack(socket, message_id, 400)
            return True
        self.last_received_at = self.app.clock.now()
        if kind == "SYSTEM" and topic == "ping":
            await self._ack(socket, message_id, 200, {"opaque": data.get("opaque")})
        elif kind == "CALLBACK" and topic == MESSAGE_TOPIC:
            try:
                await self._message(data, ingress)
            except Exception:
                await self._ack(socket, message_id, 500)
                raise
            await self._ack(socket, message_id, 200)
        else:
            await self._ack(socket, message_id, 404)
        return True

    @staticmethod
    async def _ack(
        socket: StreamSocket, message_id: str, code: int, data: dict[str, Any] | None = None
    ) -> None:
        await socket.send(
            json.dumps(
                {
                    "code": code,
                    "message": "OK" if code == 200 else "Not accepted",
                    "headers": {"messageId": message_id, "contentType": "application/json"},
                    "data": json.dumps({"response": None} if data is None else data),
                }
            )
        )

    async def _message(self, data: dict[str, Any], ingress: Ingress) -> None:
        if data.get("conversationType") != "1":
            return  # Groups are not an owner's private control channel.
        corp = _identifier(data.get("senderCorpId"))
        staff = _identifier(data.get("senderStaffId"))
        mid = _identifier(data.get("msgId"))
        if (
            not corp
            or not staff
            or not mid
            or data.get("chatbotCorpId") != corp
            or data.get("robotCode") != self.robot_code
            or (self.cfg.corp_id is not None and corp != self.cfg.corp_id)
        ):
            return
        recipient = recipient_key(self.client_id, corp, staff)
        pairing = self.app.pairing
        if not pairing.is_approved(self.name, recipient):
            await self._stranger(recipient, staff, str(data.get("senderNick") or ""))
            return
        external_id = message_key(self.client_id, corp, mid)
        text = data.get("text")
        if (
            data.get("msgtype") != "text"
            or not isinstance(text, dict)
            or not isinstance(text.get("content"), str)
        ):
            self._notice(
                recipient,
                external_id,
                "This DingTalk channel accepts text. Use the web page for pictures and voice.",
            )
            ingress.wake()
            return
        # This branch is text-only: Ingress cannot perform transcription or other network work
        # inside the transaction. Commands, their dedupe marker and their reply commit together.
        with self.app.db.tx():
            pairing.touch(self.name, recipient)
            result = await ingress.owner_message(
                channel=self.name,
                text=text["content"],
                external_id=external_id,
                recipient=recipient,
            )
            if result.status == "rejected" and result.reply:
                self._notice(recipient, external_id, result.reply)
        if result.status == "rejected":
            ingress.wake()

    def _notice(self, recipient: str, external_id: str, text: str) -> None:
        self.app.outbox.enqueue(
            session_key=DEFAULT_SESSION,
            channel=self.name,
            recipient=recipient,
            text=text,
            kind="notice",
            dedupe_key=f"dingtalk-notice:{external_id}",
            extra={"interactive": True},
        )

    async def _stranger(self, recipient: str, staff: str, label: str) -> None:
        request = self.app.pairing.request(self.name, recipient, label)
        if request is None or request.notified_at is not None:
            return
        text = (
            "This is a private assistant. On the computer running it, enter:\n\n"
            f"pa channels approve dingtalk {request.code}\n\n"
            f"The pairing code expires in {int(PAIRING_TTL_S // 60)} minutes."
        )
        try:
            await self.api.send_message(self.robot_code, staff, text, markdown=False)
        except (ChannelError, DeliveryUncertain):
            log.warning("Could not deliver a DingTalk pairing code; the next message can retry")
            return
        self.app.pairing.mark_notified(request.code)
