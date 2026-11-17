"""Authenticated outbound HTTPS. Credentials never appear in a URL or error message."""

from __future__ import annotations

import ipaddress
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class TransportError(RuntimeError):
    pass


class AuthorizationError(TransportError):
    pass


class Transport(Protocol):
    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


def control_url(raw: str) -> str:
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid control-plane URL") from exc
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("control URL must not contain credentials, a query or a fragment")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("nodes require HTTPS; HTTP is supported only on loopback")
    if parsed.path not in {"", "/"}:
        raise ValueError("control URL must be an origin without a path")
    return raw.rstrip("/")


@dataclass(frozen=True)
class Credentials:
    url: str
    node_id: str
    token: str = field(repr=False)


def save_credentials(home: Path, credentials: Credentials) -> None:
    home.mkdir(parents=True, mode=0o700, exist_ok=True)
    home.chmod(0o700)
    path = home / "node.json"
    fd, pending_name = tempfile.mkstemp(prefix="node-", suffix=".pending", dir=home)
    pending = Path(pending_name)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(
                {
                    "url": control_url(credentials.url),
                    "node_id": credentials.node_id,
                    "token": credentials.token,
                },
                stream,
            )
            stream.flush()
            os.fsync(stream.fileno())
        # Publish without replacing another node's identity, even if two pairing attempts race.
        os.link(pending, path, follow_symlinks=False)
    finally:
        pending.unlink(missing_ok=True)


def load_credentials(home: Path) -> Credentials:
    fd = os.open(home / "node.json", os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        value = json.load(stream)
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("node_id"), str)
        or not isinstance(value.get("token"), str)
        or len(value["token"]) < 32
    ):
        raise ValueError("invalid node credentials; pair this node again")
    return Credentials(control_url(value["url"]), value["node_id"], value["token"])


class HttpTransport:
    def __init__(
        self, url: str, token: str = "", *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.url = control_url(url)
        self._client = httpx.AsyncClient(
            base_url=self.url,
            timeout=httpx.Timeout(35, connect=10),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={
                "Origin": self.url,
                "X-PA": "1",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )

    async def __aenter__(self) -> HttpTransport:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if not path.startswith("/api/v3/nodes/") or "://" in path or "?" in path:
            raise ValueError("invalid node endpoint")
        try:
            async with self._client.stream(method, path, json=payload) as response:
                if response.status_code in {401, 403}:
                    raise AuthorizationError("node authorization is no longer valid")
                if not 200 <= response.status_code < 300:
                    raise TransportError(f"control request failed (HTTP {response.status_code})")
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise TransportError("control response exceeds the transfer limit")
        except httpx.HTTPError as exc:
            raise TransportError(
                "control plane is unreachable; existing attempts remain recorded"
            ) from exc
        try:
            body = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise TransportError("control plane returned invalid JSON") from exc
        if not isinstance(body, dict):
            raise TransportError("control plane returned an invalid response")
        return body
