"""`pa webhooks`: what may call the agent, whether it is set up, and a way to try a route.

Nothing here writes a file. A secret is made and shown for you to put in `.env` yourself, so it
never lands somewhere you did not look.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.markup import escape

from personal_agent.app import App
from personal_agent.channels.webhooks import (
    SCHEMES,
    WebhookService,
    new_secret,
    route_url,
    sign,
)
from personal_agent.channels.webhooks.service import REFUSED_KEY
from personal_agent.db import loads

WHERE_TO_PUT_IT = {
    "github": "the webhook's Secret field (Settings > Webhooks), with content type application/json",
    "gitlab": "the webhook's Secret token field (this scheme is weaker: prefer a `standard` signer)",
    "standard": "the signing secret of any sender that follows the Standard Webhooks / Svix headers",
    "generic": "your own script: X-Webhook-Timestamp and X-Webhook-Signature (see the README)",
}
SAMPLE_BODY = {"action": "test", "note": "sent by `pa webhooks send`"}


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def _last_delivery(opened: App, name: str, now: float) -> str:
    row = opened.db.one(
        "SELECT created_at, status FROM events WHERE source = ? ORDER BY id DESC LIMIT 1",
        (f"webhook:{name}",),
    )
    if row is None:
        return "never"
    total = opened.db.scalar(
        "SELECT COUNT(*) FROM events WHERE source = ? AND created_at > ?",
        (f"webhook:{name}", now - 86400),
        default=0,
    )
    return f"{_ago(now - row['created_at'])} ago ({row['status']}); {total} in the last day"


def _last_refusal(opened: App, name: str, now: float) -> str:
    note = loads(opened.db.kv_get(f"{REFUSED_KEY}{name}") or "", {})
    if not note:
        return "none"
    return f"{_ago(now - float(note.get('at', now)))} ago: {note.get('why', '?')} ({note.get('total', '?')} in all)"


def _body(text: str | None) -> bytes:
    if text is None:
        return json.dumps(SAMPLE_BODY).encode()
    if text.startswith("@"):
        return Path(text[1:]).expanduser().read_bytes()
    return text.encode()


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    hooks_app = typer.Typer(
        help="What may call the agent from outside, and a way to try it.",
        invoke_without_command=True,
    )

    @hooks_app.callback()
    def _list(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        opened = open_app()
        try:
            cfg = opened.config.webhooks
            if not cfg.enabled:
                console.print(
                    "webhooks   [dim]off[/dim] (\\[webhooks] enabled = true, plus a route; "
                    "see config.toml)"
                )
                return
            service = WebhookService(opened)
            now = opened.clock.now()
            listening = f"{cfg.host}:{cfg.port}"
            console.print(f"webhooks   on, listening on {escape(listening)} when `pa serve` runs")
            if not cfg.routes:
                console.print(
                    "[yellow]No route is configured:[/yellow] add \\[webhooks.routes.NAME]."
                )
                return
            for name, route in service.routes.items():
                state = "[green]ready[/green]" if route.usable else "[red]not ready[/red]"
                console.print(f"\n[bold]{escape(name)}[/bold]  {route.config.scheme}  {state}")
                console.print(f"  address        {escape(route_url(opened, name))}", soft_wrap=True)
                if not route.usable:
                    console.print(f"  [red]{escape(route.problem)}[/red]", soft_wrap=True)
                console.print(f"  events         {escape(', '.join(route.config.events) or 'all')}")
                console.print(f"  last delivery  {_last_delivery(opened, name, now)}")
                refusal = _last_refusal(opened, name, now)
                if refusal != "none":
                    console.print(f"  [yellow]last refusal   {escape(refusal)}[/yellow]")
            console.print()
            if not cfg.public_url:
                console.print(
                    "[dim]To receive deliveries from the internet, put a tunnel in front of this "
                    "listener and set public_url under \\[webhooks].[/dim]"
                )
        finally:
            opened.close()

    @hooks_app.command("secret")
    def secret(
        name: str | None = typer.Argument(None, help="A route from config.toml."),
        scheme: str | None = typer.Option(
            None, "--scheme", help=f"Without a route: {' | '.join(SCHEMES)}."
        ),
    ) -> None:
        """Make a secret for a route and say where it goes. Nothing is written."""
        opened = open_app()
        try:
            env_name = "WEBHOOK_SECRET"
            if name is not None:
                route = opened.config.webhooks.routes.get(name)
                if route is None:
                    known = ", ".join(opened.config.webhooks.routes) or "none yet"
                    console.print(
                        f"[red]No route called {escape(name)}.[/red] Routes: {escape(known)}."
                    )
                    raise typer.Exit(1)
                scheme, env_name = route.scheme, route.secret_env
            if scheme not in SCHEMES:
                console.print(f"Give a route name, or --scheme {' | '.join(SCHEMES)}.")
                raise typer.Exit(2)
            value = new_secret(scheme)
            console.print(
                f"Put this line in {escape(str(opened.paths.env_file))}:\n", soft_wrap=True
            )
            console.print(f"  {escape(env_name)}={escape(value)}", soft_wrap=True, highlight=False)
            console.print(f"\nGive the same secret to the sender: {WHERE_TO_PUT_IT[scheme]}.")
            console.print("[dim]It is shown only now; make another any time.[/dim]")
        finally:
            opened.close()

    @hooks_app.command("send")
    def send(
        name: str = typer.Argument(..., help="A route from config.toml."),
        event: str | None = typer.Option(
            None, "--event", "-e", help="The event type to claim (github: ping, push, ...)."
        ),
        body: str | None = typer.Option(
            None, "--body", "-b", help="The request body: text, or @file. Default: a small sample."
        ),
        url: str | None = typer.Option(
            None, "--url", help="Where to send it (default: the local listener)."
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask before waking the agent."),
    ) -> None:
        """Send a correctly signed delivery to a route, to try it. A real event wakes the agent."""
        opened = open_app()
        try:
            cfg = opened.config.webhooks
            route = cfg.routes.get(name)
            if route is None:
                console.print(f"[red]No route called {escape(name)}.[/red]")
                raise typer.Exit(1)
            service = WebhookService(opened)
            if not service.routes[name].usable:
                console.print(
                    f"[red]This route cannot be used:[/red] {escape(service.routes[name].problem)}"
                )
                raise typer.Exit(1)
            secret_value = os.environ[route.secret_env]  # usable, so it is set
            payload = _body(body)
            wakes = not (route.scheme == "github" and (event or "ping") == "ping")
            if wakes and not yes:
                console.print(
                    "This is a real event: the agent will run and it costs a few cents. "
                    "(`--event ping` on a github route only checks the secret and address.)"
                )
                if not typer.confirm("Send it?"):
                    raise typer.Exit(1)
            target = url or f"http://{_loopback(cfg.host)}:{cfg.port}/hook/{name}"
            headers = sign(route.scheme, secret_value, payload, now=time.time(), event=event or "")
            headers["Content-Type"] = "application/json"
            try:
                reply = httpx.post(target, content=payload, headers=headers, timeout=15.0)
            except httpx.HTTPError as exc:
                console.print(
                    f"[red]Could not reach {escape(target)}:[/red] {escape(type(exc).__name__)}. "
                    "Is `pa serve` running?"
                )
                raise typer.Exit(1) from exc
            console.print(f"{reply.status_code} {escape(reply.text.strip()[:200])}")
            if reply.status_code == 404:
                console.print(
                    "[dim]404 is what a refusal looks like from outside. The reason is in the log "
                    "and in `pa webhooks`.[/dim]"
                )
            if reply.status_code >= 400:
                raise typer.Exit(1)
        finally:
            opened.close()

    return hooks_app


def _loopback(host: str) -> str:
    if host in ("0.0.0.0", "", "::"):  # noqa: S104 - comparing, not binding
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host
