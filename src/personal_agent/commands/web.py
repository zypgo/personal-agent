"""`pa web`: the chat page, and which browsers may open it.

Getting a browser in happens here, at the computer, and nowhere else: `pa web pair` makes a link
that works once. Nobody who can only reach the page can make themselves a link, and neither can
anything the agent reads.
"""

from __future__ import annotations

import io
import webbrowser
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.channels.web.auth import TooManyInvites
from personal_agent.commands.common import console, echo, fail, opened, say, span, table
from personal_agent.presence import daemon_state

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.channels.web.auth import WebSession

app = typer.Typer(
    help="The chat page and the browsers paired with it.", invoke_without_command=True
)

TAILSCALE_HINT = (
    "To use it from your phone, put HTTPS in front of it, for example with Tailscale:\n"
    "  tailscale serve --bg 8765\n"
    'then set `public_url = "https://<this-mac>.<tailnet>.ts.net"` under [web] in config.toml.'
)


def qr_text(url: str) -> str:
    """The link as a QR code made of block characters, for a phone's camera."""
    import qrcode

    code = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(url)
    code.make(fit=True)
    out = io.StringIO()
    code.print_ascii(out=out, invert=True)  # light on a dark terminal, the usual case
    return out.getvalue()


def _reachable_from_a_phone(url: str) -> bool:
    return not url.startswith(("http://127.", "http://localhost", "http://[::1]"))


def _require_enabled(agent: App) -> None:
    if not agent.config.web.enabled:
        fail(
            "The web page is switched off.",
            "Add this to config.toml, then run `pa serve`:\n  [web]\n  enabled = true",
        )


def _browser(session: WebSession, now: float) -> tuple[str, ...]:
    state = session.state(now)
    return (
        str(session.id),
        escape(session.label or "-"),
        escape(session.agent or "-"),
        f"{span(now - session.created_at)} ago",
        f"{span(now - session.last_seen_at)} ago",
        state if state != "active" else f"active, {span(session.expires_at - now)} left",
    )


@app.callback()
def _status(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        cfg = agent.config.web
        if not cfg.enabled:
            say("web page   [dim]off[/dim] (\\[web] enabled = true)")
            return
        running = daemon_state(agent).kind == "daemon"
        serving = "serving" if running else "[yellow]not serving: run `pa serve`[/yellow]"
        say(f"web page   on, {serving}")
        say(f"listens on {escape(cfg.host)}:{cfg.port}")
        say(f"address    {escape(cfg.address)}")
        browsers = agent.web_auth.sessions()
        waiting = agent.web_auth.pending_invites()
        say(f"browsers   {len(browsers)} paired, {len(waiting)} link(s) waiting")
        if not browsers:
            say("[dim]Pair one with `pa web pair`.[/dim]")
        if not _reachable_from_a_phone(cfg.address):
            say(f"[dim]{escape(TAILSCALE_HINT)}[/dim]")


@app.command("pair")
def pair(
    label: Annotated[
        str, typer.Option("--label", "-l", help="A name for this browser, such as 'phone'.")
    ] = "",
    qr: Annotated[bool, typer.Option("--qr/--no-qr", help="Show the link as a QR code.")] = True,
) -> None:
    """Make a link that lets one browser in, once."""
    with opened() as agent:
        _require_enabled(agent)
        try:
            invite = agent.web_auth.invite(label)
        except TooManyInvites as exc:
            fail(f"{exc}.", "`pa web unpair` cancels them.")
        url = f"{agent.config.web.address}/#pair={invite.code}"
        minutes = max(1, round((invite.expires_at - agent.clock.now()) / 60))
        say(f"Open this link in the browser to pair (it works once, for {minutes} minutes):")
        say()
        say(f"  {escape(url)}")
        say()
        if _reachable_from_a_phone(url):
            if qr:
                echo(qr_text(url))
        else:
            say(
                "[yellow]That address only works on this computer.[/yellow]\n"
                f"{escape(TAILSCALE_HINT)}"
            )
        if daemon_state(agent).kind != "daemon":
            say("[yellow]Nothing is serving the page yet:[/yellow] run `pa serve`.")
        say(
            "[dim]Anyone who opens the link first gets in: do not post it anywhere. "
            "`pa web sessions` shows who did.[/dim]"
        )


@app.command("unpair")
def unpair() -> None:
    """Cancel every link that has not been used yet."""
    with opened() as agent:
        cancelled = agent.web_auth.cancel_invites()
        say(f"Cancelled {cancelled} link(s)." if cancelled else "No link was waiting.")


@app.command("open")
def open_page(
    label: Annotated[
        str, typer.Option("--label", "-l", help="A name for this browser.")
    ] = "this computer",
) -> None:
    """Open the page in this computer's browser, already paired."""
    with opened() as agent:
        _require_enabled(agent)
        invite = agent.web_auth.invite(label)
        url = f"{agent.config.web.address}/#pair={invite.code}"
        if webbrowser.open(url):
            say("Opened the page.")
        else:
            say("Could not start a browser. Open this link yourself:")
            say(f"  {escape(url)}")


@app.command("sessions")
def sessions(
    everything: Annotated[
        bool, typer.Option("--all", "-a", help="Include ended sessions.")
    ] = False,
) -> None:
    """Browsers that are paired with the page."""
    with opened() as agent:
        now = agent.clock.now()
        found = agent.web_auth.sessions(include_ended=everything)
        if not found:
            say("[dim]No browser is paired.[/dim] `pa web pair` makes a link.")
            return
        rows = table("id", "name", "browser", "paired", "last used", "state")
        for item in found:
            rows.add_row(*_browser(item, now))
        console.print(rows)


@app.command("revoke")
def revoke(
    session_id: Annotated[int | None, typer.Argument(help="The id from `pa web sessions`.")] = None,
    everything: Annotated[bool, typer.Option("--all", help="Sign out every browser.")] = False,
) -> None:
    """Sign a browser out for good (its cookie stops working at once)."""
    if (session_id is None) == (not everything):
        fail("Give a session id, or --all.", code=2)
    with opened() as agent:
        if everything:
            say(f"Signed out {agent.web_auth.revoke_all()} browser(s).")
        elif session_id is not None and agent.web_auth.revoke(session_id):
            say("Signed out.")
        else:
            fail("No such active session.", "`pa web sessions` lists them.")
