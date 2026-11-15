"""`pa web`: the chat page, and which browsers may open it.

Getting a browser in happens here, at the computer, and nowhere else: `pa web pair` makes a link
that works once. Nobody who can only reach the page can make themselves a link, and neither can
anything the agent reads.
"""

from __future__ import annotations

import io
import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.app import App
from personal_agent.channels.web.auth import TooManyInvites
from personal_agent.channels.web.channel import base_url

if TYPE_CHECKING:
    from personal_agent.channels.web.auth import WebSession

TAILSCALE_HINT = (
    "To use it from your phone, put HTTPS in front of it, for example with Tailscale:\n"
    "  tailscale serve --bg 8765\n"
    'then set `public_url = "https://<this-mac>.<tailnet>.ts.net"` under [web] in config.toml.'
)


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


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


def _require_enabled(opened: App, console: Console) -> None:
    if opened.config.web.enabled:
        return
    console.print(
        "[red]The web page is switched off.[/red] Add this to config.toml, then run `pa serve`:\n"
        "  \\[web]\n  enabled = true"
    )
    raise typer.Exit(1)


def _browser(session: WebSession, now: float) -> tuple[str, ...]:
    return (
        str(session.id),
        escape(session.label or "-"),
        escape(session.agent or "-"),
        f"{_span(now - session.created_at)} ago",
        f"{_span(now - session.last_seen_at)} ago",
        session.state(now)
        if session.state(now) != "active"
        else f"active, {_span(session.expires_at - now)} left",
    )


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    web_app = typer.Typer(
        help="The chat page and the browsers paired with it.", invoke_without_command=True
    )

    @web_app.callback()
    def _status(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        opened = open_app()
        try:
            from personal_agent.daemon import daemon_state

            cfg = opened.config.web
            if not cfg.enabled:
                console.print("web page   [dim]off[/dim] (\\[web] enabled = true)")
                return
            running = daemon_state(opened).kind == "daemon"
            serving = "serving" if running else "[yellow]not serving: run `pa serve`[/yellow]"
            console.print(f"web page   on, {serving}")
            console.print(f"listens on {escape(cfg.host)}:{cfg.port}")
            console.print(f"address    {escape(base_url(opened))}")
            browsers = opened.web_auth.sessions()
            waiting = opened.web_auth.pending_invites()
            console.print(f"browsers   {len(browsers)} paired, {len(waiting)} link(s) waiting")
            if not browsers:
                console.print("[dim]Pair one with `pa web pair`.[/dim]")
            if not _reachable_from_a_phone(base_url(opened)):
                console.print(f"[dim]{escape(TAILSCALE_HINT)}[/dim]")
        finally:
            opened.close()

    @web_app.command("pair")
    def pair(
        label: str = typer.Option(
            "", "--label", "-l", help="A name for this browser, such as 'phone'."
        ),
        qr: bool = typer.Option(True, "--qr/--no-qr", help="Show the link as a QR code."),
    ) -> None:
        """Make a link that lets one browser in, once."""
        opened = open_app()
        try:
            _require_enabled(opened, console)
            try:
                invite = opened.web_auth.invite(label)
            except TooManyInvites as exc:
                console.print(f"[red]{escape(str(exc))}.[/red] `pa web unpair` cancels them.")
                raise typer.Exit(1) from exc
            url = f"{base_url(opened)}/#pair={invite.code}"
            minutes = max(1, round((invite.expires_at - opened.clock.now()) / 60))
            console.print(
                f"Open this link in the browser to pair (it works once, for {minutes} minutes):"
            )
            console.print()
            console.print(f"  {escape(url)}", soft_wrap=True, highlight=False)
            console.print()
            if _reachable_from_a_phone(url):
                if qr:
                    console.print(qr_text(url), highlight=False, markup=False)
            else:
                console.print(
                    "[yellow]That address only works on this computer.[/yellow]\n"
                    f"{escape(TAILSCALE_HINT)}"
                )
            from personal_agent.daemon import daemon_state

            if daemon_state(opened).kind != "daemon":
                console.print("[yellow]Nothing is serving the page yet:[/yellow] run `pa serve`.")
            console.print(
                "[dim]Anyone who opens the link first gets in: do not post it anywhere. "
                "`pa web sessions` shows who did.[/dim]"
            )
        finally:
            opened.close()

    @web_app.command("unpair")
    def unpair() -> None:
        """Cancel every link that has not been used yet."""
        opened = open_app()
        try:
            cancelled = opened.web_auth.cancel_invites()
            console.print(
                f"Cancelled {cancelled} link(s)." if cancelled else "No link was waiting."
            )
        finally:
            opened.close()

    @web_app.command("open")
    def open_page(
        label: str = typer.Option(
            "this computer", "--label", "-l", help="A name for this browser."
        ),
    ) -> None:
        """Open the page in this computer's browser, already paired."""
        opened = open_app()
        try:
            _require_enabled(opened, console)
            invite = opened.web_auth.invite(label)
            url = f"{base_url(opened)}/#pair={invite.code}"
            if not webbrowser.open(url):
                console.print("Could not start a browser. Open this link yourself:")
                console.print(f"  {escape(url)}", soft_wrap=True, highlight=False)
            else:
                console.print("Opened the page.")
        finally:
            opened.close()

    @web_app.command("sessions")
    def sessions(
        everything: bool = typer.Option(False, "--all", "-a", help="Include ended sessions."),
    ) -> None:
        """Browsers that are paired with the page."""
        opened = open_app()
        try:
            now = opened.clock.now()
            rows = opened.web_auth.sessions(include_ended=everything)
            if not rows:
                console.print("[dim]No browser is paired.[/dim] `pa web pair` makes a link.")
                return
            table = Table(box=None, pad_edge=False)
            for column in ("id", "name", "browser", "paired", "last used", "state"):
                table.add_column(column)
            for item in rows:
                table.add_row(*_browser(item, now))
            console.print(table)
        finally:
            opened.close()

    @web_app.command("revoke")
    def revoke(
        session_id: int | None = typer.Argument(None, help="The id from `pa web sessions`."),
        everything: bool = typer.Option(False, "--all", help="Sign out every browser."),
    ) -> None:
        """Sign a browser out for good (its cookie stops working at once)."""
        if (session_id is None) == (not everything):
            console.print("Give a session id, or --all.")
            raise typer.Exit(2)
        opened = open_app()
        try:
            if everything:
                count = opened.web_auth.revoke_all()
                console.print(f"Signed out {count} browser(s).")
            elif opened.web_auth.revoke(int(session_id)):  # type: ignore[arg-type]
                console.print("Signed out.")
            else:
                console.print(
                    "[yellow]No such active session.[/yellow] `pa web sessions` lists them."
                )
                raise typer.Exit(1)
        finally:
            opened.close()

    return web_app
