"""The few DingTalk OpenAPI operations the channel needs, with explicit delivery outcomes.

Stream receives messages; replies and proactive notifications use OpenAPI. No sessionWebhook
bearer URL is stored or followed. An uncertain send never silently retries or changes transport.
See docs/DINGTALK.md for the official protocol and API references.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from personal_agent.channels.base import ChannelError

API_ORIGIN = "https://api.dingtalk.com"
MESSAGE_TOPIC = "/v1.0/im/bot/messages/get"
OPEN_PATH = "/v1.0/gateway/connections/open"
TOKEN_PATH = "/v1.0/oauth2/accessToken"  # noqa: S105 - an API path, not a token
SEND_PATH = "/v1.0/robot/oToMessages/batchSend"
TOKEN_HEADER = "x-acs-dingtalk-access-token"  # noqa: S105 - a header name
_AUTH_CODES = frozenset(
    {
        "InvalidAuthentication",
        "InvalidAuthentication.AccessTokenExpired",
        "InvalidAuthentication.InvalidAccessToken",
    }
)


class DingTalkApiError(ChannelError):
    """DingTalk explicitly refused an operation, or no message request was sent."""

    def __init__(
        self,
        detail: str,
        *,
        status: int = 0,
        code: str = "",
        permanent: bool = False,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(detail, permanent=permanent, retry_after=retry_after)
        self.status, self.code = status, code

    @property
    def expired_token(self) -> bool:
        return self.status == 401 or self.code in _AUTH_CODES


class DeliveryUncertain(Exception):
    """A send may have reached DingTalk. The dispatcher must not blindly send it again."""


def _code(body: dict[str, Any]) -> str:
    value = str(body.get("code") or "")
    # Keep only a bounded identifier for classification. Even a code can repeat credentials,
    # so response fields must not be interpolated into logged exception messages.
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", value) else ""


def _retry_after(response: httpx.Response) -> float:
    try:
        value = float(response.headers.get("Retry-After", "60"))
    except ValueError:
        return 60.0
    return max(1.0, min(value, 3600.0)) if math.isfinite(value) else 60.0


def stream_url(endpoint: object, ticket: object) -> str:
    """Use the platform-issued endpoint, with credentials confined to DingTalk TLS hosts."""
    if not isinstance(endpoint, str) or not isinstance(ticket, str) or not ticket:
        raise DingTalkApiError("DingTalk returned an incomplete Stream connection")
    try:
        parts = urlsplit(endpoint)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise DingTalkApiError("DingTalk returned an invalid Stream endpoint") from None
    if (
        parts.scheme != "wss"
        or not host.endswith(".dingtalk.com")
        or port not in (None, 443)
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
        or not parts.path.startswith("/")
    ):
        raise DingTalkApiError("DingTalk returned an untrusted Stream endpoint", permanent=True)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode({"ticket": ticket}), ""))


class DingTalkApi:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        client: httpx.AsyncClient | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client_id = client_id
        self._secret = client_secret
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=False,
            headers={"User-Agent": "personal-agent-dingtalk/1"},
        )
        self._now = monotonic
        self._token = ""
        self._token_until = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._token = ""
        await self._client.aclose()

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None = None,
        sending: bool = False,
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                API_ORIGIN + path,
                json=payload,
                headers={TOKEN_HEADER: token} if token else None,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise DingTalkApiError(
                f"could not connect to DingTalk ({type(exc).__name__})"
            ) from None
        except httpx.HTTPError as exc:
            detail = f"DingTalk connection interrupted ({type(exc).__name__})"
            if sending:
                raise DeliveryUncertain(detail) from None
            raise DingTalkApiError(detail) from None
        try:
            raw = response.json()
            body = raw if isinstance(raw, dict) else {}
        except ValueError:
            body = {}
        status, code = response.status_code, _code(body)
        if sending and (status == 408 or (200 <= status < 400 and status not in (200, 201))):
            # An unexpected success/redirect can follow processing of a POST. Refusing to
            # follow the redirect prevents a credential leak, but doesn't prove non-delivery.
            raise DeliveryUncertain(f"DingTalk returned HTTP {status}; delivery is unknown")
        if status >= 500:
            if sending:
                raise DeliveryUncertain(f"DingTalk returned HTTP {status}; delivery is unknown")
            raise DingTalkApiError(f"DingTalk returned HTTP {status}", status=status)
        if status == 429:
            raise DingTalkApiError(
                "DingTalk rate limit reached",
                status=status,
                code=code,
                retry_after=_retry_after(response),
            )
        if status not in (200, 201) or (code and code != "0"):
            raise DingTalkApiError(
                f"DingTalk refused the request (HTTP {status})",
                status=status,
                code=code,
                permanent=status in (400, 401, 403, 404),
            )
        return body

    async def access_token(self) -> str:
        async with self._token_lock:
            if self._token and self._now() < self._token_until:
                return self._token
            body = await self._post(
                TOKEN_PATH, {"appKey": self.client_id, "appSecret": self._secret}
            )
            token, expiry = body.get("accessToken"), body.get("expireIn")
            if (
                not isinstance(token, str)
                or not token
                or not isinstance(expiry, (int, float))
                or isinstance(expiry, bool)
                or not math.isfinite(expiry)
                or expiry <= 0
            ):
                raise DingTalkApiError("DingTalk returned an invalid access token response")
            self._token, self._token_until = (
                token,
                self._now() + max(0.0, expiry - min(60.0, expiry / 10)),
            )
            return token

    async def open_connection(self) -> str:
        body = await self._post(
            OPEN_PATH,
            {
                "clientId": self.client_id,
                "clientSecret": self._secret,
                "subscriptions": [{"type": "CALLBACK", "topic": MESSAGE_TOPIC}],
                "ua": "personal-agent-python/1",
            },
        )
        return stream_url(body.get("endpoint"), body.get("ticket"))

    async def send_message(
        self, robot_code: str, staff_id: str, text: str, *, markdown: bool = True
    ) -> str:
        """Send to one staff ID; a batch-level receipt alone does not mean that user was accepted."""
        params = {"title": "Personal Agent", "text": text} if markdown else {"content": text}
        payload = {
            "robotCode": robot_code,
            "userIds": [staff_id],
            "msgKey": "sampleMarkdown" if markdown else "sampleText",
            "msgParam": json.dumps(params, ensure_ascii=False),
        }
        for attempt in range(2):
            token = await self.access_token()  # Failing here cannot have sent an IM message.
            try:
                body = await self._post(SEND_PATH, payload, token=token, sending=True)
            except DingTalkApiError as exc:
                if attempt == 0 and exc.expired_token:
                    # A concurrent sender may already have refreshed it. Never invalidate its token.
                    if self._token == token:
                        self._token, self._token_until = "", 0.0
                    continue
                raise
            for key in ("invalidStaffIdList", "filteredStaffIdList", "flowControlledStaffIdList"):
                values = body.get(key)
                if values is None:  # Optional result lists may be omitted or null.
                    values = []
                if not isinstance(values, list) or any(
                    not isinstance(value, str) for value in values
                ):
                    raise DeliveryUncertain("DingTalk returned an invalid recipient result")
                if staff_id in values:
                    limited = key == "flowControlledStaffIdList"
                    raise DingTalkApiError(
                        "DingTalk rate-limited this recipient"
                        if limited
                        else "DingTalk refused this recipient",
                        permanent=not limited,
                        retry_after=60.0 if limited else None,
                    )
            receipt = body.get("processQueryKey")
            if not isinstance(receipt, str) or not receipt:
                raise DeliveryUncertain("DingTalk did not return a delivery receipt")
            return receipt
        raise AssertionError("token retry loop must return or raise")
