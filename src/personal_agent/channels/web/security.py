"""What every request to the web page must pass before any of our code looks at it.

The page can run the agent, and a browser will happily send a page's requests to any address,
including this one, on behalf of whichever site the owner has open. So the checks live in one
place, in front of everything:

* **Host.** The `Host` header must be a name this server is meant to be reached under. This is what
  stops DNS rebinding: a hostile site whose name suddenly points at 127.0.0.1 still sends its own
  name in `Host`.
* **Origin, exactly.** A cookie belongs to a *host*, not to a port, so a page served from
  `localhost:3000` is "same site" as this one and its requests carry our cookie. Only the full
  origin (scheme, host and port) tells them apart, so a request that names an origin must name one
  of ours, and every request that changes something must name one.
* **Fetch metadata.** Browsers say whether a request came from another site (`Sec-Fetch-Site`). API
  requests are refused unless they came from this page or from the owner typing the address.
* **A header a form cannot send.** State-changing requests also need `X-PA`, which makes a browser
  ask permission first (a preflight) for any other origin, and this server never gives it.
* **Size.** A request body is cut off at a limit while it is read, not after.

And every response gets headers that stop the browser doing things we never meant: running anything
but our own scripts, loading anything from elsewhere (which is also how an injected "image" would
carry a secret out), framing the page, sniffing a type, or leaking the address.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from urllib.parse import urlsplit

from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' blob: data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self'",
        "font-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)
"""Nothing loads from anywhere but this server. In particular `img-src` has no `https:`: a picture
that the agent was talked into printing, with the owner's data in its address, goes nowhere."""

SECURITY_HEADERS = {
    "content-security-policy": CSP,
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
    "referrer-policy": "no-referrer",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "permissions-policy": "camera=(), geolocation=(), microphone=(self), payment=(), usb=()",
}

CUSTOM_HEADER = "x-pa"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
SAME_ORIGIN = frozenset({"same-origin", "none"})
DEFAULT_BODY_LIMIT = 64 * 1024
WORKER_PATH = re.compile(r"/api/v4/agents/[A-Za-z0-9_.:-]+/worker/[^?#]*\Z")


def hostname(value: str | None) -> str:
    """The host part of a `Host` header (no port), lowercase; brackets kept for IPv6."""
    if not value:
        return ""
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


def allowed_hosts(host: str, public_url: str | None, extra: Iterable[str] = ()) -> frozenset[str]:
    """The names the page may be reached under: this machine, the public address, and what the
    owner listed. A server listening on a specific address answers to that address too."""
    names = {"localhost", "127.0.0.1", "[::1]"}
    if host not in ("0.0.0.0", "::", ""):  # noqa: S104 - comparing, not binding
        names.add(hostname(f"[{host}]" if ":" in host else host))
    if public_url:
        names.add(hostname(urlsplit(public_url).netloc))
    names.update(hostname(name) for name in extra)
    return frozenset(names)


def allowed_origins(
    host: str, port: int, public_url: str | None, extra_hosts: Iterable[str] = ()
) -> frozenset[str]:
    """Full origins, port included. Both spellings of this machine, the listener's own address, and
    the public address; nothing else, however similar it looks."""
    origins = {f"http://localhost:{port}", f"http://127.0.0.1:{port}", f"http://[::1]:{port}"}
    if host not in ("0.0.0.0", "::", "", "localhost", "127.0.0.1", "::1"):  # noqa: S104
        origins.add(f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}")
    if public_url:
        origins.add(public_url.rstrip("/").lower())
    for name in extra_hosts:
        origins.add(f"http://{name.lower()}:{port}")
        origins.add(f"https://{name.lower()}")
    return frozenset(origins)


def _refuse(status: int, message: str) -> tuple[int, bytes]:
    return status, json.dumps({"error": message}).encode()


class Guard:
    """ASGI middleware: the checks above, then the response headers."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        hosts: frozenset[str],
        origins: frozenset[str],
        body_limit: Callable[[str, str], int] = lambda method, path: DEFAULT_BODY_LIMIT,
        api_prefix: str = "/api/",
        health_host: str | None = None,
    ) -> None:
        self.app = app
        self.hosts = hosts
        self.origins = origins
        self.body_limit = body_limit
        self.api_prefix = api_prefix
        self.health_host = health_host

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self.app(scope, receive, send)
            return
        verdict = self._check(scope)
        if verdict is not None:
            await self._respond(send, *verdict)
            return
        limit = self.body_limit(scope["method"], scope["path"])
        headers = Headers(scope=scope)
        declared = headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._respond(send, *_refuse(413, "That is too large."))
            return
        await self.app(scope, _limited(receive, limit), self._with_headers(send))

    def _check(self, scope: Scope) -> tuple[int, bytes] | None:
        headers = Headers(scope=scope)
        health = (
            self.health_host is not None
            and hostname(headers.get("host")) == self.health_host
            and scope["path"] == "/healthz"
            and scope["method"] in ("GET", "HEAD")
        )
        if hostname(headers.get("host")) not in self.hosts and not health:
            return _refuse(421, "This address is not served here.")
        origin = headers.get("origin")
        if origin is not None and origin.lower() not in self.origins:
            return _refuse(403, "This page does not accept requests from there.")
        site = headers.get("sec-fetch-site")
        method = scope["method"]
        # Native API/MCP clients carry explicit per-agent credentials, never browser cookies.
        # Endpoints authenticate before reading bodies; Host, supplied Origin and Fetch
        # Metadata checks remain in force for this narrow machine interface.
        machine = (
            WORKER_PATH.fullmatch(scope["path"]) is not None
            and headers.get("authorization", "").startswith("Bearer ")
            and not headers.get("cookie")
        )
        if method not in SAFE_METHODS:
            if origin is None and not machine:
                return _refuse(403, "A request that changes something must say where it came from.")
            if site is not None and site not in SAME_ORIGIN:
                return _refuse(403, "This page does not accept requests from other sites.")
            if headers.get(CUSTOM_HEADER) != "1" and not machine:
                return _refuse(403, "Missing the header that marks a request from the page itself.")
        elif (
            scope["path"].startswith(self.api_prefix)
            and site is not None
            and site not in SAME_ORIGIN
        ):
            return _refuse(403, "This page does not accept requests from other sites.")
        return None

    async def _respond(self, send: Send, status: int, body: bytes) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"cache-control", b"no-store"),
                    (b"content-length", str(len(body)).encode()),
                    *[(k.encode(), v.encode()) for k, v in SECURITY_HEADERS.items()],
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    def _with_headers(self, send: Send) -> Send:
        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                for key, value in SECURITY_HEADERS.items():
                    if key not in headers:  # a route may be stricter (an image is sandboxed)
                        headers[key] = value
            await send(message)

        return wrapped


def _limited(receive: Receive, limit: int) -> Receive:
    """`receive`, but a body that grows past `limit` stops the request (413) however it is sent."""
    seen = 0

    async def limited() -> Message:
        nonlocal seen
        message = await receive()
        if message["type"] == "http.request":
            seen += len(message.get("body", b""))
            if seen > limit:
                raise HTTPException(413, "That is too large.")
        return message

    return limited
