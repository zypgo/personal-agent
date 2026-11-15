"""Webhooks: other programs wake the agent.

GitHub, a build server, a script of yours: each calls `POST /hook/NAME` on a listener of its own
(never the one that serves the chat page: this is the one you may have to put on the internet).

What a delivery has to get past, in this order, and what the sender is told when it does not:

    a route that exists, with a secret that works      404, and nothing is read
    a body within the size limit, sent in time         413, 408
    a signature only the sender can make               404, the same as for no such route
    GitHub's `ping`                                    200, nothing is queued
    the route's `events` list                          202 "ignored"
    the route's rate limit                             429, with Retry-After
    the inbox, which has seen this delivery before     202 "duplicate"; otherwise 202 "queued"

The event it becomes is untrusted. The run that handles it can only look and tell the owner, and
what the sender wrote reaches the model as fenced data. The owner's own words about this kind of
event (`prompt` in the route) travel with it as trusted text, so the run knows what to do without
having to believe anything the delivery says.

Why the sender is told so little: a stranger probing `/hook/...` should learn nothing, not even
that a route exists. The reason for every refusal goes to the log instead, and `pa webhooks` shows
the latest one.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from starlette.types import Receive, Scope, Send

from personal_agent.channels.http import HttpListener
from personal_agent.channels.webhooks.signatures import (
    Rejected,
    SecretProblem,
    fallback_id,
    norm_event,
    secret_key,
    verify,
)
from personal_agent.core.events import AddResult, Kind, NewEvent, Trust

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.config import WebhookRoute
    from personal_agent.daemon import Daemon

log = logging.getLogger(__name__)

READ_TIMEOUT_S = 15.0
"""A sender that has not finished its request by then is dropped: a few connections that send a
byte at a time would otherwise hold every slot of a listener that has no one in front of it."""
RATE_WINDOW_S = 60.0
NOTE_EVERY_S = 10.0
"""How often refusals are written to the log (and remembered for `pa webhooks`): a scan of the
internet must not fill the log or the database."""
MAX_CONNECTIONS = 50
ROUTE_PATH = re.compile(r"/hook/([a-z0-9][a-z0-9_-]{0,31})/?")
REFUSED_KEY = "webhooks:refused:"
"""kv key prefix: `{prefix}{route}` holds the latest refusal, for `pa webhooks`."""

SubmitFn = Callable[[NewEvent], AddResult]


@dataclass(frozen=True)
class Reply:
    status: int
    body: dict[str, Any]
    headers: tuple[tuple[str, str], ...] = ()


NOT_FOUND = Reply(404, {"error": "not found"})
TOO_LARGE = Reply(413, {"error": "payload too large"}, (("connection", "close"),))
TOO_SLOW = Reply(408, {"error": "request timeout"}, (("connection", "close"),))


@dataclass
class Route:
    name: str
    config: WebhookRoute
    key: bytes | None
    """What the secret stands for, or None if it cannot be used (then the route is not served)."""
    problem: str = ""
    accepted_at: deque[float] = field(default_factory=deque)
    refused: int = 0
    unnoted: int = 0
    noted_at: float = float("-inf")

    @property
    def usable(self) -> bool:
        return self.key is not None


def route_secret(config: WebhookRoute, env: Mapping[str, str]) -> tuple[bytes | None, str]:
    """What a route's secret stands for, or why it cannot be used (`pa doctor` asks the same)."""
    secret = env.get(config.secret_env, "")
    if not secret.strip():
        return None, f"{config.secret_env} is not set (put it in .env)"
    try:
        return secret_key(config.scheme, secret), ""
    except SecretProblem as exc:
        return None, f"{config.secret_env}: {exc}"


def route_url(app: App, name: str) -> str:
    """The address a sender is given for a route: the public address if there is one."""
    cfg = app.config.webhooks
    base = cfg.public_url or f"http://{_display_host(cfg.host)}:{cfg.port}"
    return f"{base.rstrip('/')}/hook/{name}"


def _display_host(host: str) -> str:
    if host in ("0.0.0.0", "", "::"):  # noqa: S104 - comparing, not binding
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


class WebhookService:
    name = "webhooks"

    def __init__(
        self,
        app: App,
        *,
        environ: Mapping[str, str] | None = None,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        self.app = app
        self.cfg = app.config.webhooks
        self.host = host or self.cfg.host
        self.port = self.cfg.port if port is None else port
        self.listener: HttpListener | None = None
        env = os.environ if environ is None else environ
        self.routes: dict[str, Route] = {
            name: self._route(name, route, env) for name, route in self.cfg.routes.items()
        }

    @staticmethod
    def _route(name: str, config: WebhookRoute, env: Mapping[str, str]) -> Route:
        key, problem = route_secret(config, env)
        return Route(name, config, key, problem)

    @property
    def problems(self) -> list[str]:
        return [f"{r.name}: {r.problem}" for r in self.routes.values() if r.problem]

    # ---- the listener -----------------------------------------------------------------------
    async def run(self, daemon: Daemon) -> None:
        """Serve until the daemon stops. Called under supervision (restarted if it crashes)."""
        for problem in self.problems:
            log.warning("webhook route switched off: %s", problem)
        served = [r.name for r in self.routes.values() if r.usable]
        if not served:
            log.warning("webhooks are enabled but no route can be served, so nothing listens")
            return
        stop = asyncio.Event()
        listener = HttpListener(
            name="webhooks",
            host=self.host,
            port=self.port,
            stopping=stop,
            max_connections=MAX_CONNECTIONS,
        )
        self.listener = listener
        try:
            port = listener.bind()  # a taken port is reported here, before anything else
            log.info("webhooks on %s:%s for %s", self.host, port, ", ".join(sorted(served)))
            await listener.serve(self.asgi(daemon.submit))
        finally:
            listener.close()
            self.listener = None

    def asgi(self, submit: SubmitFn) -> Callable[..., Any]:
        """The server itself: one POST route, and a 404 for everything else."""

        async def handler(scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http":
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                return
            reply = await self._serve(scope, receive, submit)
            if reply is not None:  # None: the sender hung up; nobody is left to answer
                await _respond(send, reply)

        return handler

    async def _serve(self, scope: Scope, receive: Receive, submit: SubmitFn) -> Reply | None:
        match = ROUTE_PATH.fullmatch(scope["path"]) if scope["method"] == "POST" else None
        route = self.routes.get(match.group(1)) if match else None
        if route is None or not route.usable:
            return NOT_FOUND
        headers = _headers(scope)
        limit = self.cfg.max_body_bytes
        declared = headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            return TOO_LARGE
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                body = await _read(receive, limit)
        except _TooLarge:
            return TOO_LARGE
        except _Gone:
            return None
        except TimeoutError:
            return TOO_SLOW
        return self.accept(route, headers, body, submit)

    # ---- one delivery -----------------------------------------------------------------------
    def accept(
        self, route: Route, headers: Mapping[str, str], body: bytes, submit: SubmitFn
    ) -> Reply:
        """Decide what to do with a complete request to a served route."""
        cfg, now = route.config, self.app.clock.now()
        assert route.key is not None
        try:
            proof = verify(
                cfg.scheme, route.key, headers, body, now=now, tolerance_s=cfg.tolerance_s
            )
        except Rejected as why:
            self._refused(route, str(why), now)
            return NOT_FOUND
        # From here on the sender is who it says it is. What it sent is still only data.
        value = parse_body(body, headers.get("content-type", ""))
        event = proof.event or (norm_event(_field(value, "type") or _field(value, "event")))
        action = norm_event(_field(value, "action"))
        if cfg.scheme == "github" and event == "ping":
            return Reply(200, {"status": "pong"})  # GitHub sends one when a webhook is created
        if not _wanted(cfg.events, event, action):
            return Reply(202, {"status": "ignored"})
        wait = self._wait(route, now)
        if wait:
            return Reply(429, {"error": "too many deliveries"}, (("retry-after", str(wait)),))
        added = submit(
            NewEvent(
                source=f"webhook:{route.name}",
                kind=Kind.WEBHOOK,
                session_key=f"webhook:{route.name}",
                payload={
                    "name": route.name,
                    "event": f"{event}.{action}" if event and action else event,
                    "body": value,
                    "instructions": cfg.prompt,
                },
                external_id=proof.delivery or fallback_id(body, now),
                trust=Trust.UNTRUSTED,
            )
        )
        if added.duplicate:
            return Reply(202, {"status": "duplicate"})
        route.accepted_at.append(now)
        return Reply(202, {"status": "queued"})

    def _wait(self, route: Route, now: float) -> int:
        """0 if another delivery fits in the route's limit; else seconds until one does."""
        recent = route.accepted_at
        while recent and now - recent[0] >= RATE_WINDOW_S:
            recent.popleft()
        if len(recent) < self.cfg.rate_per_minute:
            return 0
        return max(1, math.ceil(RATE_WINDOW_S - (now - recent[0])))

    def _refused(self, route: Route, why: str, now: float) -> None:
        route.refused += 1
        route.unnoted += 1
        if now - route.noted_at < NOTE_EVERY_S:
            return
        log.warning(
            "webhook %s: refused a delivery (%s); %d refused since the last note",
            route.name,
            why,
            route.unnoted,
        )
        route.unnoted, route.noted_at = 0, now
        note = json.dumps({"at": now, "why": why, "total": route.refused})
        with contextlib.suppress(Exception):  # a busy database must not cost the sender an answer
            self.app.db.kv_set(f"{REFUSED_KEY}{route.name}", note, now=now)


# ---- reading the request -------------------------------------------------------------------
class _TooLarge(Exception):
    pass


class _Gone(Exception):
    pass


def _headers(scope: Scope) -> dict[str, str]:
    """Header names in lower case. One sent twice is joined with ", " as HTTP says, which no
    signature check will accept."""
    found: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers") or []:
        name, value = raw_name.decode("latin-1").lower(), raw_value.decode("latin-1")
        found[name] = f"{found[name]}, {value}" if name in found else value
    return found


async def _read(receive: Receive, limit: int) -> bytes:
    """The whole body, however it is sent, or `_TooLarge` the moment it passes `limit`."""
    parts: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            raise _Gone
        if message["type"] != "http.request":
            continue
        part = message.get("body", b"")
        size += len(part)
        if size > limit:
            raise _TooLarge
        parts.append(part)
        if not message.get("more_body", False):
            return b"".join(parts)


async def _respond(send: Send, reply: Reply) -> None:
    body = json.dumps(reply.body).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
        *[(k.encode(), v.encode()) for k, v in reply.headers],
    ]
    await send({"type": "http.response.start", "status": reply.status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


# ---- reading what was sent -----------------------------------------------------------------
MAX_DEPTH = 64
"""How deeply JSON may nest and still be read as JSON. Real payloads are a dozen levels deep; a body
that goes further is an attempt to make whatever handles it run out of stack, and is kept as text."""


def parse_body(body: bytes, content_type: str) -> Any:
    """What the sender sent, as data: JSON if it is JSON (GitHub can also wrap it in a form field
    called `payload`), else the text."""
    text = body.decode("utf-8", errors="replace")
    if content_type.split(";", 1)[
        0
    ].strip().lower() == "application/x-www-form-urlencoded" and text.startswith("payload="):
        text = parse_qs(text, keep_blank_values=True, max_num_fields=50).get("payload", [""])[0]
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        return text
    return value if _shallow(value) else text


def _shallow(value: Any) -> bool:
    """Is `value` at most `MAX_DEPTH` levels deep? Looks without recursing."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if not isinstance(item, dict | list):
            continue
        if depth > MAX_DEPTH:
            return False
        children = item.values() if isinstance(item, dict) else item
        stack.extend((child, depth + 1) for child in children)
    return True


def _field(value: Any, name: str) -> str | None:
    found = value.get(name) if isinstance(value, dict) else None
    return found if isinstance(found, str) else None


def _wanted(events: list[str], event: str, action: str) -> bool:
    """Does the route's `events` list want this? Empty wants everything. A name matches the event
    (`pull_request`) or the event with its action (`pull_request.opened`)."""
    if not events:
        return True
    wanted = {name for name in map(norm_event, events) if name}
    return event in wanted or bool(action and f"{event}.{action}" in wanted)
