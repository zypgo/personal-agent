"""The web page's server side, on a test client: no sockets, no daemon."""

from __future__ import annotations

import asyncio
from typing import Any

from starlette.testclient import TestClient

from personal_agent.app import App
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import build_web_app
from personal_agent.channels.web.security import allowed_hosts, allowed_origins

PORT = 8765
ORIGIN = f"http://127.0.0.1:{PORT}"
WRITE = {"Origin": ORIGIN, "X-PA": "1"}
"""What the page itself adds to a request that changes something."""

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x01" * 64


class Web:
    """The page's server with a browser-like client attached."""

    def __init__(self, app: App, *, public_url: str | None = None, extra_hosts=()) -> None:
        self.app = app
        self.stop = asyncio.Event()
        self.woken = 0
        secure = bool(public_url and public_url.startswith("https://"))
        self.origin = public_url or ORIGIN
        asgi = build_web_app(
            app,
            Ingress(app, wake=self._wake),
            hosts=allowed_hosts("127.0.0.1", public_url, extra_hosts),
            origins=allowed_origins("127.0.0.1", PORT, public_url, extra_hosts),
            stop=self.stop,
            secure=secure,
            wake=self._wake,
        )
        self.client = TestClient(asgi, base_url=self.origin, follow_redirects=False)
        self.write = {"Origin": self.origin, "X-PA": "1"}

    def _wake(self) -> None:
        self.woken += 1

    # ---- being signed in ----------------------------------------------------------------
    def pair(self, label: str = "phone"):
        code = self.app.web_auth.invite(label).code
        response = self.client.post("/api/pair", json={"code": code}, headers=self.write)
        assert response.status_code == 200, response.text
        return response

    def token(self) -> str:
        return self.client.cookies.get("pa_session") or ""

    # ---- what the page does ---------------------------------------------------------------
    def send(
        self,
        text: str = "",
        *,
        images: list[tuple[bytes, str]] = (),
        voice: tuple[bytes, str] | None = None,
        voice_seconds: float | None = None,
        client_id: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        data: dict[str, Any] = {"text": text}
        if client_id:
            data["client_id"] = client_id
        if voice_seconds is not None:
            data["voice_seconds"] = str(voice_seconds)
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("image", (f"photo{i}", body, mime)) for i, (body, mime) in enumerate(images)
        ]
        if voice is not None:
            files.append(("voice", ("note.webm", voice[0], voice[1])))
        return self.client.post(
            "/api/messages",
            data=data,
            files=files or {"x": (None, "")},
            headers=headers or self.write,
        )

    def events(self) -> list[dict]:
        import json

        return [
            {**dict(r), "payload": json.loads(r["payload"])}
            for r in self.app.db.query("SELECT * FROM events ORDER BY id")
        ]
