"""The web page as a channel: a server the daemon runs, and a place the agent's messages go.

Delivery here means something different from a chat app's. The page does not receive messages; it
reads the owner's history (the `transcript` table), and the dispatcher writes a message there the
moment `send` returns. So `send` has nothing to do and nothing that can fail: what it confirms is
that the message is where the page will show it, whether or not a browser happens to be open.

That is also why this channel is "pull only" (see `PULL_ONLY`): a message waits until the owner
opens the page, nothing taps their shoulder. Unprompted news therefore prefers a chat app that can.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from personal_agent.channels.http import HttpListener
from personal_agent.channels.ingress import Ingress
from personal_agent.channels.web.api import build_web_app
from personal_agent.channels.web.security import allowed_hosts, allowed_origins
from personal_agent.core.outbox import OutMessage

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.daemon import Daemon

log = logging.getLogger(__name__)


def base_url(app: App) -> str:
    """The address to open the page at: the public address if there is one, else this machine."""
    cfg = app.config.web
    return cfg.public_url or f"http://127.0.0.1:{cfg.port}"


class WebChannel:
    name = "web"

    def __init__(self, app: App, *, host: str | None = None, port: int | None = None) -> None:
        self.app = app
        cfg = app.config.web
        self.host = host or cfg.host
        self.port = cfg.port if port is None else port
        self.listener: HttpListener | None = None

    async def send(self, message: OutMessage) -> None:
        """Nothing to deliver: the dispatcher records the message in the transcript, which is what
        the page shows. Returning means it is there."""

    async def run(self, daemon: Daemon) -> None:
        """Serve the page until the daemon stops. Called under supervision (restarted on a crash)."""
        app, cfg = self.app, self.app.config.web
        stop = asyncio.Event()
        listener = HttpListener(
            name="web",
            host=self.host,
            port=self.port,
            stopping=stop,
            on_stop=lambda: app.live.publish({"type": "stop"}),  # wakes every open stream
        )
        self.listener = listener
        try:
            port = listener.bind()  # a taken port is reported here, before anything else
            asgi = build_web_app(
                app,
                Ingress(app, wake=daemon.wake),
                hosts=allowed_hosts(self.host, cfg.public_url, cfg.allowed_hosts),
                origins=allowed_origins(self.host, port, cfg.public_url, cfg.allowed_hosts),
                stop=stop,
                secure=bool(cfg.public_url and cfg.public_url.startswith("https://")),
                wake=daemon.wake,
            )
            log.info("web page at %s (listening on %s:%s)", base_url(app), self.host, port)
            await listener.serve(asgi)
        finally:
            listener.close()
            self.listener = None
