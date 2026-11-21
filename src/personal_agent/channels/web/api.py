"""The web page's server side: a handful of routes on Starlette, behind `Guard`.

Everything under `/api/` except pairing and a challenge-only health proof needs a session cookie, and the session is looked up
before a request's body is touched. The page itself and its files are public (they contain
nothing private: the agent's name, its messages and its state are only ever sent through `/api/`).

Messages the owner writes go through `Ingress`, the same door Telegram uses; the answers come back
through the outbox like any other channel's, and reach the page as events on `/api/stream`. The
page replays durable history on reconnect. In-progress answer previews are separate, bounded
in-memory snapshots; they never advance the durable history cursor.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from personal_agent.channels.commands import ChatCommands
from personal_agent.channels.ingress import MAX_IMAGES, Ingress, Upload
from personal_agent.channels.web.artifacts import artifact_routes
from personal_agent.channels.web.assistant import assistant_routes
from personal_agent.channels.web.connect import connection_info
from personal_agent.channels.web.probe import probe_signature
from personal_agent.channels.web.security import Guard
from personal_agent.collaboration.models import AccessDenied
from personal_agent.core.activity import activity_snapshot
from personal_agent.core.approvals import Approval, plain
from personal_agent.core.transcript import Turn

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.channels.web.auth import WebSession

log = logging.getLogger(__name__)

COOKIE = "pa_session"
STATIC_DIR = Path(__file__).parent / "static"
TICK_S = 3.0
"""How often an open stream looks at the database even when nothing nudged it."""
PING_S = 15.0
"""A stream that has said nothing for this long sends a comment line, so a proxy or a phone's
network does not decide it is dead (and so a closed page is noticed)."""
SESSION_CHECK_S = 30.0
PAIR_ATTEMPTS = (10, 60.0)
"""At most this many failed pairing attempts per this many seconds from one address, and then it
waits. The codes cannot be guessed; this only keeps a noisy stranger from making work, so it must
not be a way to keep the owner out: other addresses are not counted with it, and a right code from
an address that has not failed is never held up."""
PAIR_SOURCES = 1000
"""How many addresses with recent failures are remembered."""
HISTORY_MAX = 100
_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{8,64}")

_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".html": "text/html; charset=utf-8",
}


@dataclass(frozen=True)
class Asset:
    body: bytes
    content_type: str
    etag: str


@dataclass(frozen=True)
class Sent:
    """A message as the page's form carried it."""

    text: str
    client_id: str
    images: list[Upload]
    voice: Upload | None


PRODUCT_NAME = "栖序"


def page_title(agent: str) -> str:
    """The tab's title: the product, with the agent's own name in front when it has one."""
    return f"{agent} · {PRODUCT_NAME}" if agent and agent != PRODUCT_NAME else PRODUCT_NAME


def load_assets(
    directory: Path = STATIC_DIR, *, agent: str = "Agent"
) -> tuple[dict[str, Asset], str]:
    """Every file of the page, read once, and a version that changes when any of them does.

    `{{version}}` in the page, its manifest and its service worker is filled in here, and
    `{{title}}` in the page: the agent's name is the one thing a person types, so it goes in last
    and escaped, and is never read as a placeholder itself."""
    found: dict[str, bytes] = {}
    for path in sorted(directory.iterdir()):
        if path.is_file() and path.suffix in _TYPES and not path.name.startswith("."):
            found[path.name] = path.read_bytes()
    digest = hashlib.sha256(agent.encode())
    for name, body in found.items():
        digest.update(name.encode() + b"\0" + body + b"\0")
    version = digest.hexdigest()[:12]
    assets: dict[str, Asset] = {}
    for name, body in found.items():
        if name in ("index.html", "sw.js", "manifest.webmanifest"):
            body = body.replace(b"{{version}}", version.encode())
        if name == "index.html":
            body = body.replace(b"{{title}}", html.escape(page_title(agent), quote=True).encode())
        assets[name] = Asset(
            body,
            _TYPES[Path(name).suffix],
            '"' + hashlib.sha256(body).hexdigest()[:20] + '"',
        )
    return assets, version


def sse(event: str, data: Any, event_id: int | None = None) -> bytes:
    """One server-sent event. JSON never contains a raw newline, so one `data:` line is enough."""
    lines = [f"id: {event_id}"] if event_id is not None else []
    lines.append(f"event: {event}")
    lines.append("data: " + json.dumps(data, ensure_ascii=False, separators=(",", ":")))
    return ("\n".join(lines) + "\n\n").encode()


class Window:
    """At most `count` hits in any `seconds`."""

    def __init__(self, count: int, seconds: float) -> None:
        self.count = count
        self.seconds = seconds
        self._hits: deque[float] = deque()

    def blocked(self, now: float) -> float | None:
        """How long until a hit would be allowed, or None if one would be. Counts nothing."""
        while self._hits and self._hits[0] <= now - self.seconds:
            self._hits.popleft()
        if len(self._hits) >= self.count:
            return self._hits[0] + self.seconds - now
        return None

    def hit(self, now: float) -> float | None:
        """None if this hit is allowed (and counted), else how long until one would be."""
        wait = self.blocked(now)
        if wait is None:
            self._hits.append(now)
        return wait


class Strikes:
    """Failed attempts counted per source: `count` in any `seconds`, then that source waits."""

    def __init__(self, count: int, seconds: float, *, remember: int = PAIR_SOURCES) -> None:
        self.count = count
        self.seconds = seconds
        self.remember = remember
        self._windows: dict[str, Window] = {}

    def wait(self, source: str, now: float) -> float | None:
        """How long until `source` may try again, or None if it may now."""
        window = self._windows.get(source)
        return window.blocked(now) if window is not None else None

    def fail(self, source: str, now: float) -> None:
        window = self._windows.get(source)
        if window is None:
            if len(self._windows) >= self.remember:
                self._forget(now)
            window = self._windows[source] = Window(self.count, self.seconds)
        window.hit(now)

    def _forget(self, now: float) -> None:
        """Make room: drop the sources that have nothing recent against them, and if there are
        none, the one that has been here longest."""
        for source in [s for s, w in self._windows.items() if w.blocked(now) is None]:
            del self._windows[source]
        if len(self._windows) >= self.remember:
            del self._windows[next(iter(self._windows))]


def approval_json(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "tool": approval.tool,
        "summary": plain(approval.summary),
        "status": approval.status,
        "created_at": approval.created_at,
        "expires_at": approval.expires_at,
    }


class WebApi:
    def __init__(
        self,
        app: App,
        ingress: Ingress,
        *,
        stop: asyncio.Event,
        secure: bool,
        wake: Callable[[], None] = lambda: None,
    ) -> None:
        self.app = app
        self.ingress = ingress
        self.stop = stop
        self.secure = secure
        self.wake = wake
        self.auth = app.web_auth
        self.assets, self.version = load_assets(agent=app.config.agent.name)
        self._pairing = Strikes(*PAIR_ATTEMPTS)

    # ---- who is asking -----------------------------------------------------------------
    def _token(self, request: Request) -> str:
        return request.cookies.get(COOKIE, "")

    def _need(self, request: Request) -> WebSession:
        session = self.auth.lookup(self._token(request))
        if session is None:
            raise HTTPException(401, "Not signed in.")
        try:
            self.app.require_actor(session.principal_id, write=False)
        except AccessDenied as exc:
            raise HTTPException(403, "Space is unavailable.") from exc
        return session

    @staticmethod
    def _json(data: Any, status: int = 200) -> JSONResponse:
        return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store"})

    # ---- the page and its files ---------------------------------------------------------
    def _asset(self, request: Request, name: str, *, cache: str = "no-cache") -> Response:
        asset = self.assets.get(name)
        if asset is None:
            raise HTTPException(404, "Not found.")
        headers = {"ETag": asset.etag, "Cache-Control": cache}
        if request.headers.get("if-none-match") == asset.etag:
            return Response(status_code=304, headers=headers)
        return Response(asset.body, media_type=asset.content_type, headers=headers)

    async def index(self, request: Request) -> Response:
        return self._asset(request, "index.html")

    async def service_worker(self, request: Request) -> Response:
        return self._asset(request, "sw.js")

    async def manifest(self, request: Request) -> Response:
        return self._asset(request, "manifest.webmanifest")

    async def static(self, request: Request) -> Response:
        return self._asset(request, request.path_params["name"])

    # ---- signing in -------------------------------------------------------------------
    async def pair(self, request: Request) -> Response:
        now = self.app.clock.now()
        source = request.client.host if request.client else ""
        wait = self._pairing.wait(source, now)
        if wait is not None:
            return JSONResponse(
                {"error": "Too many attempts. Wait a moment."},
                status_code=429,
                headers={"Retry-After": str(int(wait) + 1), "Cache-Control": "no-store"},
            )
        try:
            body = await request.json()
        except (ValueError, RecursionError):
            self._pairing.fail(source, now)
            raise HTTPException(400, "That was not a pairing request.") from None
        code = body.get("code") if isinstance(body, dict) else None
        if not isinstance(code, str):
            self._pairing.fail(source, now)
            raise HTTPException(400, "That was not a pairing request.")
        found = self.auth.redeem(code, agent=request.headers.get("user-agent", ""))
        if found is None:
            self._pairing.fail(source, now)
            raise HTTPException(
                403,
                "This pairing link has expired or was already used. "
                "Run 'pa web pair' on the computer for a new one.",
            )
        response = self._json({"ok": True, "label": found.session.label})
        response.set_cookie(
            COOKIE,
            found.token,
            max_age=int(self.auth.session_s),
            path="/",
            httponly=True,
            samesite="strict",
            secure=self.secure,
        )
        return response

    async def logout(self, request: Request) -> Response:
        token = self._token(request)
        if self.auth.lookup(token) is None:
            raise HTTPException(401, "Not signed in.")
        self.auth.revoke_token(token)
        response = self._json({"ok": True})
        response.delete_cookie(
            COOKIE, path="/", httponly=True, samesite="strict", secure=self.secure
        )
        return response

    async def session(self, request: Request) -> Response:
        session = self._need(request)
        cfg = self.app.config
        return self._json(
            {
                "principal_id": session.principal_id,
                "space_id": self.app.space_id,
                "agent": cfg.agent.name,
                "owner": cfg.agent.owner_name,
                "label": session.label,
                "last_id": self.app.transcript.last_id(),
                "version": self.version,
                "features": {
                    "images": cfg.media.images,
                    "voice": cfg.media.voice,
                    "voice_seconds": cfg.media.max_voice_seconds,
                    "max_images": MAX_IMAGES,
                    "max_image_bytes": cfg.media.max_image_bytes,
                    "max_voice_bytes": cfg.media.max_voice_bytes,
                },
                "connection": connection_info(cfg.web),
            }
        )

    # ---- the conversation --------------------------------------------------------------
    def turn_json(self, turn: Turn) -> dict[str, Any]:
        data = turn.to_json()
        ids = turn.meta.get("approval_ids")
        if isinstance(ids, list) and ids:
            found = [self.app.approvals.get(int(i)) for i in ids[:10] if str(i).isdigit()]
            data["approvals"] = [approval_json(a) for a in found if a is not None]
        return data

    async def history(self, request: Request) -> Response:
        self._need(request)
        try:
            limit = min(max(int(request.query_params.get("limit", "40")), 1), HISTORY_MAX)
            before = request.query_params.get("before")
            before_id = int(before) if before else None
        except ValueError:
            raise HTTPException(400, "limit and before are numbers.") from None
        turns = self.app.transcript.latest(limit + 1, before=before_id)
        more = len(turns) > limit
        return self._json({"turns": [self.turn_json(t) for t in turns[-limit:]], "more": more})

    async def _read_message(self, request: Request) -> Sent:
        """What the page's form holds, read in full and closed again.

        Uploads arrive as temporary files; leaving the form open would keep them (and any disk
        they spilled onto) until the garbage collector got round to them.
        """
        async with request.form(max_files=MAX_IMAGES + 2, max_fields=12) as form:

            def text_field(name: str) -> str:
                value = form.get(name)
                return value if isinstance(value, str) else ""

            async def upload(item: Any) -> Upload | None:
                if not hasattr(item, "read"):
                    return None
                return Upload(
                    await item.read(), mime=item.content_type or "", name=item.filename or ""
                )

            images = [u for u in [await upload(i) for i in form.getlist("image")] if u is not None]
            voice = await upload(form.get("voice")) if form.get("voice") is not None else None
            try:
                seconds = float(text_field("voice_seconds")) if voice is not None else None
            except ValueError:
                seconds = None
            if voice is not None and seconds and seconds > 0:
                voice = Upload(voice.data, mime=voice.mime, name=voice.name, duration_s=seconds)
            client_id = text_field("client_id")
            if not _CLIENT_ID.fullmatch(client_id):
                client_id = uuid.uuid4().hex
            return Sent(text_field("text"), client_id, images, voice)

    async def messages(self, request: Request) -> Response:
        session = self._need(request)  # before parsing uploads
        try:
            authority = self.app.actor_snapshot(session.principal_id)
        except AccessDenied as exc:
            raise HTTPException(403, "Space is unavailable.") from exc
        if not request.headers.get("content-type", "").startswith("multipart/form-data"):
            raise HTTPException(415, "Send the message as a form.")
        sent = await self._read_message(request)
        self._need(request)  # Upload reading can overlap device or membership revocation.
        try:
            accepted = await self.ingress.owner_message(
                channel="web",
                text=sent.text,
                images=sent.images,
                voice=sent.voice,
                external_id=f"web:{session.principal_id}:{sent.client_id}",
                principal_id=session.principal_id,
                client_request_id=sent.client_id,
                membership_version=authority.get("membership_version"),
                principal_version=authority.get("principal_version"),
            )
        except AccessDenied as exc:
            raise HTTPException(403, "Authorization changed; send a new message.") from exc
        if accepted.status == "rejected":
            return self._json({"status": "rejected", "error": accepted.reply}, 422)
        return self._json(
            {
                "status": accepted.status,
                "turn": self.turn_json(accepted.turn) if accepted.turn else None,
            }
        )

    async def media(self, request: Request) -> Response:
        self._need(request)
        found = self.app.media.read(request.path_params["media_id"])
        if found is None:
            raise HTTPException(404, "No such picture.")
        data, mime = found
        return Response(
            data,
            media_type=mime,
            headers={
                "Cache-Control": "private, no-store",
                "Content-Disposition": "inline",
                # Whatever the bytes turn out to be, they may not run anything or load anything.
                "Content-Security-Policy": "default-src 'none'; sandbox",
            },
        )

    # ---- approvals and status ---------------------------------------------------------
    async def approvals(self, request: Request) -> Response:
        self._need(request)
        return self._json({"pending": [approval_json(a) for a in self.app.approvals.pending()]})

    async def decide(self, request: Request) -> Response:
        session = self._need(request)
        try:
            self.app.require_approval_actor(session.principal_id)
            authority = self.app.actor_snapshot(session.principal_id)
        except AccessDenied as exc:
            raise HTTPException(403, "Only this space's owners can approve actions.") from exc
        try:
            approval_id = int(request.path_params["approval_id"])
            body = await request.json()
        except (ValueError, RecursionError):
            raise HTTPException(400, "That was not a decision.") from None
        decision = body.get("decision") if isinstance(body, dict) else None
        if decision not in ("approve", "deny"):
            raise HTTPException(400, 'decision is "approve" or "deny".')
        session = self._need(request)
        try:
            self.app.require_approval_actor(session.principal_id)
            self.app.require_actor(
                session.principal_id,
                version=authority.get("membership_version"),
                principal_version=authority.get("principal_version"),
            )
        except AccessDenied as exc:
            raise HTTPException(403, "Only this space's owners can approve actions.") from exc
        note = body.get("note") if isinstance(body.get("note"), str) else None
        result = self.app.approvals.decide(
            approval_id,
            decision == "approve",
            by=f"{session.principal_id}:web",
            note=(note or "")[:500] or None,
        )
        if result.resume_queued:
            self.wake()
        return self._json(
            {
                "ok": result.changed,
                "message": result.message,
                "approval": approval_json(result.approval) if result.approval else None,
            }
        )

    async def status(self, request: Request) -> Response:
        self._need(request)
        app = self.app
        budget = app.budget.status()
        return self._json(
            {
                "working": app.live.busy,
                "pending_approvals": len(app.approvals.pending()),
                "spent_today": round(budget.spent_today, 4),
                "daily_limit": budget.daily_limit,
                "text": ChatCommands(app).status(),
                "activity": activity_snapshot(app),
                "connection": connection_info(app.config.web),
            }
        )

    async def activity(self, request: Request) -> Response:
        self._need(request)
        return self._json(activity_snapshot(self.app))

    async def ready(self, request: Request) -> Response:
        self.app.db.one("SELECT 1")
        return self._json({"ok": True})

    async def health(self, request: Request) -> Response:
        # A local setup command proves the listener belongs to its home before configuring a
        # proxy. This response contains only a signature of caller-supplied random bytes.
        challenge = request.query_params.get("challenge", "")
        if not re.fullmatch(r"[0-9a-f]{32}", challenge):
            raise HTTPException(400, "A fresh probe challenge is required.")
        return self._json({"proof": probe_signature(self.app.db, challenge)})

    # ---- live updates -------------------------------------------------------------------
    def _recent_approvals(self) -> list[dict[str, Any]]:
        return [approval_json(a) for a in self.app.approvals.recent(20)]

    async def stream(self, request: Request) -> Response:
        session = self._need(request)
        membership_version = (
            self.app.identities.require_access(session.principal_id, self.app.space_id).version
            if self.app.identities is not None
            else None
        )
        raw = request.headers.get("last-event-id") or request.query_params.get("after") or ""
        last_known = self.app.transcript.last_id()
        try:
            after = min(max(int(raw), 0), last_known) if raw else last_known
        except ValueError:
            raise HTTPException(400, "after is a number.") from None
        return StreamingResponse(
            self._events(request, session, after, membership_version=membership_version),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",  # a reverse proxy must not hold the events back
            },
        )

    async def _events(
        self,
        request: Request,
        session: WebSession,
        after: int,
        *,
        membership_version: int | None = None,
    ) -> AsyncIterator[bytes]:
        app = self.app
        token = self._token(request)
        sub = app.live.subscribe()

        def authorize() -> None:
            fresh = self.auth.lookup(token)
            if fresh is None or fresh.id != session.id:
                raise AccessDenied("Not signed in.")
            app.require_actor(
                session.principal_id,
                write=False,
                version=membership_version,
                principal_version=session.authz_version,
            )

        def event_frame(event: str, data: Any, event_id: int | None = None) -> bytes:
            # Every yield may wait on a slow browser. Recheck each frame, including frames
            # already collected in this batch, before any more private content leaves.
            authorize()
            return sse(event, data, event_id)

        try:
            if membership_version is None and app.identities is not None:
                membership_version = app.identities.require_access(
                    session.principal_id, app.space_id
                ).version
            authorize()
            yield b"retry: 3000\n\n"
            approvals = self._recent_approvals()
            drafts = app.live.response_drafts()
            yield event_frame(
                "hello",
                {
                    "working": app.live.busy,
                    "last_id": app.transcript.last_id(),
                    "approvals": approvals,
                    "drafts": drafts,
                },
            )
            last_sent = after
            quiet_s = 0.0
            while not self.stop.is_set():
                event = await sub.next(TICK_S)
                if self.stop.is_set():
                    break
                authorize()  # Quiet streams also close promptly after revocation.
                spoke = False
                for turn in app.transcript.after(last_sent):
                    last_sent = turn.id
                    yield event_frame("turn", self.turn_json(turn), turn.id)
                    spoke = True
                latest = None
                assistant_changed = False
                for item in ([event] if event else []) + sub.drain():
                    if item.get("type") == "activity":
                        latest = item
                    elif (
                        item.get("type") == "assistant_changed"
                        and item.get("principal_id", session.principal_id) == session.principal_id
                    ):
                        assistant_changed = True
                if latest is not None:
                    yield event_frame(
                        "activity",
                        {"working": bool(latest.get("working")), "tool": latest.get("tool")},
                    )
                    spoke = True
                if assistant_changed:
                    # A nudge carries no proposal, credential or task content. The browser
                    # reloads its own authorized cards; durable state survives missed nudges.
                    yield event_frame("assistant_changed", {})
                    spoke = True
                # Snapshot replacement tolerates missed nudges and a sleeping phone. Previews
                # have no SSE id: only committed transcript turns advance Last-Event-ID.
                current_drafts = app.live.response_drafts()
                if current_drafts != drafts:
                    drafts = current_drafts
                    yield event_frame("drafts", {"drafts": drafts})
                    spoke = True
                # Approvals are decided from other places too (Telegram, the terminal, which is
                # another process): compare with the database rather than wait for a nudge.
                current = self._recent_approvals()
                if current != approvals:
                    approvals = current
                    yield event_frame("approvals", {"approvals": approvals})
                    spoke = True
                if await request.is_disconnected():
                    return
                quiet_s = 0.0 if spoke else quiet_s + TICK_S
                if quiet_s >= PING_S:
                    quiet_s = 0.0
                    authorize()
                    yield b": ping\n\n"
        except AccessDenied:
            yield sse("signout", {})
        finally:
            sub.close()

    # ---- wiring -----------------------------------------------------------------------
    def routes(self) -> list[Route]:
        return [
            Route("/", self.index),
            Route("/sw.js", self.service_worker),
            Route("/manifest.webmanifest", self.manifest),
            Route("/static/{name}", self.static),
            Route("/api/pair", self.pair, methods=["POST"]),
            Route("/api/logout", self.logout, methods=["POST"]),
            Route("/api/session", self.session),
            Route("/api/history", self.history),
            Route("/api/stream", self.stream),
            Route("/api/messages", self.messages, methods=["POST"]),
            Route("/api/media/{media_id}", self.media),
            *artifact_routes(self.app),
            Route("/api/approvals", self.approvals),
            Route("/api/approvals/{approval_id}", self.decide, methods=["POST"]),
            Route("/api/status", self.status),
            Route("/api/activity", self.activity),
            *assistant_routes(self),
            Route("/api/health", self.health),
            Route("/healthz", self.ready),
        ]


def body_limit_for(app: App) -> Callable[[str, str], int]:
    """How much body each route may receive: a message may carry pictures and a recording; nothing
    else sends more than a few lines of JSON."""
    media = app.config.media
    message = MAX_IMAGES * media.max_image_bytes + media.max_voice_bytes + 256 * 1024

    def limit(method: str, path: str) -> int:
        if method == "POST" and path.startswith("/api/v4/"):
            if "/worker/" in path and (path.endswith("/result") or path.endswith("/mcp")):
                return 16 * 1024 * 1024
            if path == "/api/v4/tasks":
                return 1024 * 1024
        if method == "POST" and path.startswith("/api/v3/nodes/attempts/"):
            return 16 * 1024 * 1024
        return message if method == "POST" and path == "/api/messages" else 64 * 1024

    return limit


async def _error(_request: Request, exc: Exception) -> Response:
    if isinstance(exc, HTTPException):
        return JSONResponse(
            {"error": exc.detail},
            status_code=exc.status_code,
            headers={"Cache-Control": "no-store"},
        )
    log.exception("web request failed", exc_info=exc)
    return JSONResponse(
        {"error": "Something went wrong. The details are in the agent's log."},
        status_code=500,
        headers={"Cache-Control": "no-store"},
    )


def build_web_app(
    app: App,
    ingress: Ingress,
    *,
    hosts: frozenset[str],
    origins: frozenset[str],
    stop: asyncio.Event,
    secure: bool,
    wake: Callable[[], None] = lambda: None,
) -> Any:
    """The ASGI application: routes, behind the request checks."""
    api = WebApi(app, ingress, stop=stop, secure=secure, wake=wake)
    inner = Starlette(
        routes=api.routes(),
        exception_handlers={HTTPException: _error, Exception: _error},
    )
    routed: Any = inner
    if app.collaboration is not None and app.collaboration.root is app:
        from personal_agent.channels.web.collaboration import SpaceRouter

        routed = SpaceRouter(inner, app, api)
    return Guard(
        routed,
        hosts=hosts,
        origins=origins,
        body_limit=body_limit_for(app),
        health_host="healthcheck.railway.app" if os.environ.get("RAILWAY_SERVICE_ID") else None,
    )
