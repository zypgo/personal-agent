"""`pa webhooks`: what may call the agent, whether it is set up, and a way to try a route.

Nothing here writes a file. A secret is made and shown for you to put in `.env` yourself, so it
never lands somewhere you did not look.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import httpx
import typer
from rich.markup import escape

from personal_agent.channels.webhooks import SCHEMES, WebhookService, new_secret, route_url, sign
from personal_agent.channels.webhooks.service import REFUSED_KEY
from personal_agent.commands.common import USAGE, fail, opened, say, span
from personal_agent.db import loads

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(
    help="What may call the agent from outside, and a way to try it.", invoke_without_command=True
)

RouteArg = Annotated[str, typer.Argument(help="A route from config.toml.")]
WHERE_TO_PUT_IT = {
    "github": "the webhook's Secret field (Settings > Webhooks), with content type application/json",
    "gitlab": "the webhook's Secret token field (this scheme is weaker: prefer a `standard` signer)",
    "standard": "the signing secret of any sender that follows the Standard Webhooks / Svix headers",
    "generic": "your own script: X-Webhook-Timestamp and X-Webhook-Signature (see the README)",
}
SAMPLE_BODY = {"action": "test", "note": "sent by `pa webhooks send`"}


def _last_delivery(agent: App, name: str, now: float) -> str:
    row = agent.db.one(
        "SELECT created_at, status FROM events WHERE source = ? ORDER BY id DESC LIMIT 1",
        (f"webhook:{name}",),
    )
    if row is None:
        return "never"
    total = agent.db.scalar(
        "SELECT COUNT(*) FROM events WHERE source = ? AND created_at > ?",
        (f"webhook:{name}", now - 86400),
        default=0,
    )
    return f"{span(now - row['created_at'])} ago ({row['status']}); {total} in the last day"


def _last_refusal(agent: App, name: str, now: float) -> str:
    note = loads(agent.db.kv_get(f"{REFUSED_KEY}{name}") or "", {})
    if not note:
        return "none"
    when = span(now - float(note.get("at", now)))
    return f"{when} ago: {note.get('why', '?')} ({note.get('total', '?')} in all)"


def _body(text: str | None) -> bytes:
    if text is None:
        return json.dumps(SAMPLE_BODY).encode()
    if text.startswith("@"):
        return Path(text[1:]).expanduser().read_bytes()
    return text.encode()


def _loopback(host: str) -> str:
    if host in ("0.0.0.0", "", "::"):  # noqa: S104 - comparing, not binding
        return "127.0.0.1"
    return f"[{host}]" if ":" in host else host


@app.callback()
def _list(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        cfg = agent.config.webhooks
        if not cfg.enabled:
            say(
                "webhooks   [dim]off[/dim] (\\[webhooks] enabled = true, plus a route; "
                "see config.toml)"
            )
            return
        service = WebhookService(agent)
        now = agent.clock.now()
        say(f"webhooks   on, listening on {escape(f'{cfg.host}:{cfg.port}')} when `pa serve` runs")
        if not cfg.routes:
            say("[yellow]No route is configured:[/yellow] add \\[webhooks.routes.NAME].")
            return
        for name, route in service.routes.items():
            state = "[green]ready[/green]" if route.usable else "[red]not ready[/red]"
            say(f"\n[bold]{escape(name)}[/bold]  {route.config.scheme}  {state}")
            say(f"  address        {escape(route_url(agent, name))}")
            if not route.usable:
                say(f"  [red]{escape(route.problem)}[/red]")
            say(f"  events         {escape(', '.join(route.config.events) or 'all')}")
            say(f"  last delivery  {_last_delivery(agent, name, now)}")
            refusal = _last_refusal(agent, name, now)
            if refusal != "none":
                say(f"  [yellow]last refusal   {escape(refusal)}[/yellow]")
        say()
        if not cfg.public_url:
            say(
                "[dim]To receive deliveries from the internet, put a tunnel in front of this "
                "listener and set public_url under \\[webhooks].[/dim]"
            )


@app.command("secret")
def secret(
    name: Annotated[str | None, typer.Argument(help="A route from config.toml.")] = None,
    scheme: Annotated[
        str | None, typer.Option("--scheme", help=f"Without a route: {' | '.join(SCHEMES)}.")
    ] = None,
) -> None:
    """Make a secret for a route and say where it goes. Nothing is written."""
    with opened() as agent:
        env_name = "WEBHOOK_SECRET"
        if name is not None:
            route = agent.config.webhooks.routes.get(name)
            if route is None:
                known = ", ".join(agent.config.webhooks.routes) or "none yet"
                fail(f"No route called {name}.", f"Routes: {known}.")
            scheme, env_name = route.scheme, route.secret_env
        if scheme not in SCHEMES:
            fail(f"Give a route name, or --scheme {' | '.join(SCHEMES)}.", code=USAGE)
        say(f"Put this line in {escape(str(agent.paths.env_file))}:\n")
        say(f"  {escape(env_name)}={escape(new_secret(scheme))}")
        say(f"\nGive the same secret to the sender: {WHERE_TO_PUT_IT[scheme]}.")
        say("[dim]It is shown only now; make another any time.[/dim]")


@app.command("send")
def send(
    name: RouteArg,
    event: Annotated[
        str | None,
        typer.Option("--event", "-e", help="The event type to claim (github: ping, push, ...)."),
    ] = None,
    body: Annotated[
        str | None,
        typer.Option(
            "--body", "-b", help="The request body: text, or @file. Default: a small sample."
        ),
    ] = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Where to send it (default: the local listener).")
    ] = None,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Do not ask before waking the agent.")
    ] = False,
) -> None:
    """Send a correctly signed delivery to a route, to try it. A real event wakes the agent."""
    with opened() as agent:
        cfg = agent.config.webhooks
        route = cfg.routes.get(name)
        if route is None:
            fail(f"No route called {name}.")
        usable = WebhookService(agent).routes[name]
        if not usable.usable:
            fail("This route cannot be used:", usable.problem)
        secret_value = os.environ[route.secret_env]  # usable, so it is set
        payload = _body(body)
        wakes = not (route.scheme == "github" and (event or "ping") == "ping")
        if wakes and not yes:
            say(
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
            fail(f"Could not reach {target}: {type(exc).__name__}.", "Is `pa serve` running?")
        say(f"{reply.status_code} {escape(reply.text.strip()[:200])}")
        if reply.status_code == 404:
            say(
                "[dim]404 is what a refusal looks like from outside. The reason is in the log "
                "and in `pa webhooks`.[/dim]"
            )
        if reply.status_code >= 400:
            raise typer.Exit(1)
