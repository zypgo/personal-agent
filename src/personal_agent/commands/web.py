"""`pa web`: the chat page, and which browsers may open it.

Getting a browser in happens here, at the computer, and nowhere else: `pa web pair` makes a link
that works once. Nobody who can only reach the page can make themselves a link, and neither can
anything the agent reads.
"""

from __future__ import annotations

import io
import ipaddress
import webbrowser
from typing import TYPE_CHECKING, Annotated
from urllib.parse import urlsplit

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
    "To connect your phone with private HTTPS:\n"
    "  pa web connect --via tailscale\n"
    "Or, on the same trusted Wi-Fi: pa web connect --via lan"
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
    host = urlsplit(url).hostname or ""
    if host.lower() == "localhost":
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return bool(host)


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
        _pair_link(agent, label, qr)


def _pair_link(agent: App, label: str, qr: bool) -> None:
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
        say(f"[yellow]That address only works on this computer.[/yellow]\n{escape(TAILSCALE_HINT)}")
    if daemon_state(agent).kind != "daemon":
        say("[yellow]Nothing is serving the page yet:[/yellow] run `pa serve`.")
    say(
        "[dim]Anyone who opens the link first gets in: do not post it anywhere. "
        "`pa web sessions` shows who did.[/dim]"
    )


@app.command("connect")
def connect(
    via: Annotated[
        str, typer.Option(help="tailscale (private HTTPS) or lan (same Wi-Fi).")
    ] = "tailscale",
    host: Annotated[
        str | None, typer.Option(help="This computer's private IPv4 address, for LAN mode.")
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(
            min=1, max=65535, help="Local web port (keeps the configured port by default)."
        ),
    ] = None,
    https_port: Annotated[
        int,
        typer.Option(
            min=1, max=65535, help="Tailscale HTTPS port; choose another if 443 is occupied."
        ),
    ] = 443,
    label: Annotated[
        str, typer.Option("--label", "-l", help="Name this phone's browser.")
    ] = "phone",
    qr: Annotated[bool, typer.Option("--qr/--no-qr", help="Print the pairing QR code.")] = True,
) -> None:
    """Configure phone access and issue a one-time pairing link."""
    from personal_agent.channels.web.connect import (
        ConnectionError,
        lan_connection,
        tailscale_connection,
    )
    from personal_agent.channels.web.probe import verify_backend
    from personal_agent.config_edit import ConfigEditError, set_section

    if via not in {"lan", "tailscale"}:
        fail("--via must be lan or tailscale.", code=2)
    if host is not None and via != "lan":
        fail("--host is only used with --via lan.", code=2)
    with opened() as agent:
        local_port = port or agent.config.web.port
        try:
            connection = (
                lan_connection(host, local_port)
                if via == "lan"
                else tailscale_connection(
                    local_port, https_port, verify_target=lambda p: verify_backend(agent.db, p)
                )
            )
            if via == "lan":
                verify_backend(agent.db, local_port, host=connection.host)
            settings = connection.settings()
            set_section(agent.paths.config_file, "web", settings)
        except (ConnectionError, ConfigEditError) as exc:
            fail(str(exc))
        agent.config = agent.config.model_copy(
            update={"web": agent.config.web.model_copy(update=settings)}
        )
        say(f"Phone access configured: {escape(connection.url)}")
        if via == "lan":
            say(
                "Use the same trusted Wi-Fi on both devices. This address uses HTTP; use Tailscale for encrypted access and microphone recording."
            )
        else:
            say(
                "Connect your phone to the same tailnet. HTTPS is private to devices allowed by your Tailscale policy; browser pairing is still required."
            )
        say(
            "Apply the settings: restart `pa serve`, or run `pa service restart` if the service is installed."
        )
        say("Then scan the code below. `pa web check` diagnoses the connection.")
        _pair_link(agent, label, qr)


@app.command("check")
def check_connection() -> None:
    """Check the listener and the configured phone address without changing anything."""
    from personal_agent.channels.web.connect import ConnectionError, connection_info
    from personal_agent.channels.web.probe import verify_endpoint

    with opened() as agent:
        _require_enabled(agent)
        cfg = agent.config.web
        info = connection_info(cfg)
        say(f"Connection: {info['via']} · {escape(cfg.address)}")
        host = (
            "127.0.0.1"
            if cfg.host in {"0.0.0.0", "localhost"}  # noqa: S104 - normalize an existing bind for a probe
            else "::1"
            if cfg.host == "::"
            else cfg.host
        )
        authority = f"[{host}]" if ":" in host else host
        local = f"http://{authority}:{cfg.port}"
        targets = [("Local listener", local)]
        if cfg.address != local:
            targets.append(("Phone address", cfg.address))
        failed = False
        for name, url in targets:
            try:
                verify_endpoint(agent.db, url)
                ok, detail = True, "this agent is responding"
            except ConnectionError as exc:
                ok, detail = False, str(exc)
            failed = failed or not ok
            say(f"{'[green]OK[/green]' if ok else '[red]FAIL[/red]'} {name}: {escape(detail)}")
        say(
            "Phone still cannot connect? Check Wi-Fi client isolation/firewall, or that Tailscale is connected on both devices. Keep the computer awake."
        )
        if failed:
            fail(
                "Connection check failed.",
                "Check the address and restart the agent after changing its settings.",
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
