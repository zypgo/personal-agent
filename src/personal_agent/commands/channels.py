"""`pa channels`: the chat channels, and who may use them.

Approving a sender happens here, at the computer, and nowhere else: a person who can only reach
the bot from a chat app cannot approve themselves, and neither can anything the bot is told.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.app import App


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    channels_app = typer.Typer(
        help="Chat channels and who may use them.", invoke_without_command=True
    )

    @channels_app.callback()
    def _list(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        opened = open_app()
        try:
            cfg = opened.config
            table = Table(box=None, pad_edge=False)
            for column in ("channel", "state", "approved", "waiting for approval"):
                table.add_column(column)
            rows: list[tuple[str, str, str, str]] = [
                ("cli", "always available in `pa chat`", "-", "-")
            ]
            telegram = cfg.telegram
            if telegram.enabled:
                state = (
                    "[green]on[/green]"
                    if os.environ.get(telegram.token_env)
                    else f"[red]on, but {escape(telegram.token_env)} is not set[/red]"
                )
            else:
                state = "[dim]off[/dim] (\\[telegram] enabled = true)"
            rows.append(
                (
                    "telegram",
                    state,
                    str(len(opened.pairing.senders("telegram"))),
                    str(len(opened.pairing.pending("telegram"))),
                )
            )
            if cfg.web.enabled:
                web_state = "[green]on[/green] (`pa web`)"
            else:
                web_state = "[dim]off[/dim] (\\[web] enabled = true)"
            rows.append(
                (
                    "web",
                    web_state,
                    str(len(opened.web_auth.sessions())),
                    str(len(opened.web_auth.pending_invites())),
                )
            )
            for name, spec in sorted(opened.plugin_channels.items()):
                rows.append(
                    (escape(name), f"[green]on[/green] (plugin {escape(spec.plugin)})", "-", "-")
                )
            for row in rows:
                table.add_row(*row)
            console.print(table)
            if opened.pairing.pending():
                console.print(
                    "[yellow]Someone is waiting to be approved:[/yellow] `pa channels pending`"
                )
        finally:
            opened.close()

    @channels_app.command("pending")
    def pending() -> None:
        """People who messaged a bot and are waiting for you to approve them."""
        opened = open_app()
        try:
            rows = opened.pairing.pending()
            if not rows:
                console.print("[dim]Nobody is waiting.[/dim]")
                return
            now = opened.clock.now()
            table = Table(box=None, pad_edge=False)
            for column in ("code", "channel", "who", "asked", "expires in"):
                table.add_column(column)
            for item in rows:
                table.add_row(
                    f"[bold]{escape(item.code)}[/bold]",
                    escape(item.channel),
                    escape(item.label or item.sender),
                    f"{_ago(now - item.created_at)} ago",
                    _ago(item.expires_at - now),
                )
            console.print(table)
            console.print("[dim]Only approve a code you saw appear on your own phone.[/dim]")
        finally:
            opened.close()

    @channels_app.command("approve")
    def approve(
        channel: str = typer.Argument(..., help="telegram"),
        code: str = typer.Argument(..., help="The code the bot sent, such as ABCD-EFGH."),
    ) -> None:
        """Let the person behind a pairing code talk to the agent."""
        opened = open_app()
        try:
            request = opened.pairing.peek(code)
            if request is None:
                console.print(
                    "[red]No such code[/red] (it may have expired: message the bot again). "
                    "`pa channels pending` lists the live ones."
                )
                raise typer.Exit(1)
            if request.channel != channel.lower():
                console.print(
                    f"[red]That code belongs to {escape(request.channel)}, not {escape(channel)}.[/red]"
                )
                raise typer.Exit(1)
            sender = opened.pairing.approve(code)
            if sender is None:  # it expired in the moment between looking and approving
                console.print(
                    "[red]That code just expired.[/red] Ask them to message the bot again."
                )
                raise typer.Exit(1)
            who = escape(sender.label or sender.sender)
            console.print(f"[green]Approved {who} on {escape(sender.channel)}.[/green]")
        finally:
            opened.close()

    @channels_app.command("deny")
    def deny(code: str = typer.Argument(..., help="The code to throw away.")) -> None:
        """Refuse a pairing request."""
        opened = open_app()
        try:
            if opened.pairing.deny(code) is None:
                console.print("[yellow]No such code.[/yellow]")
                raise typer.Exit(1)
            console.print("Refused.")
        finally:
            opened.close()

    @channels_app.command("allow")
    def allow(
        channel: str = typer.Argument(..., help="telegram"),
        sender: str = typer.Argument(..., help="The account id (a Telegram user id)."),
        label: str = typer.Option("", "--label", "-l", help="A name for your own reference."),
    ) -> None:
        """Approve an account whose id you already know, without a code."""
        opened = open_app()
        try:
            opened.pairing.allow(channel.lower(), sender, label)
            console.print(f"[green]Approved {escape(sender)} on {escape(channel)}.[/green]")
        finally:
            opened.close()

    @channels_app.command("senders")
    def senders() -> None:
        """Accounts that may talk to the agent."""
        opened = open_app()
        try:
            rows = opened.pairing.senders()
            if not rows:
                console.print("[dim]No account is approved on any chat channel.[/dim]")
                return
            now = opened.clock.now()
            table = Table(box=None, pad_edge=False)
            for column in ("channel", "id", "name", "approved", "last heard"):
                table.add_column(column)
            for item in rows:
                table.add_row(
                    escape(item.channel),
                    escape(item.sender),
                    escape(item.label),
                    f"{_ago(now - item.approved_at)} ago",
                    f"{_ago(now - item.last_seen_at)} ago" if item.last_seen_at else "-",
                )
            console.print(table)
        finally:
            opened.close()

    @channels_app.command("revoke")
    def revoke(
        channel: str = typer.Argument(..., help="telegram"),
        sender: str = typer.Argument(..., help="The account id from `pa channels senders`."),
    ) -> None:
        """Stop an account from talking to the agent."""
        opened = open_app()
        try:
            if not opened.pairing.revoke(channel.lower(), sender):
                console.print("[yellow]That account was not approved.[/yellow]")
                raise typer.Exit(1)
            console.print("Revoked.")
        finally:
            opened.close()

    @channels_app.command("test")
    def test(channel: str = typer.Argument("telegram", help="telegram")) -> None:
        """Check that a channel can reach its service (Telegram: asks the bot who it is)."""
        if channel.lower() != "telegram":
            console.print(f"[red]No test for {escape(channel)}.[/red]")
            raise typer.Exit(2)
        opened = open_app()
        try:
            raise typer.Exit(asyncio.run(_test_telegram(opened, console)))
        finally:
            opened.close()

    return channels_app


async def _test_telegram(opened: App, console: Console) -> int:
    from personal_agent.channels.base import ChannelError
    from personal_agent.channels.telegram import TelegramChannel
    from personal_agent.channels.telegram.api import NotSent, TelegramApiError

    cfg = opened.config.telegram
    channel = TelegramChannel(opened)
    try:
        me = await channel.api.get_me()
    except ChannelError as exc:
        console.print(f"[red]{escape(str(exc))}[/red]")
        console.print(f"Put {escape(cfg.token_env)}=... in {escape(str(opened.paths.env_file))}")
        return 1
    except TelegramApiError as exc:
        if exc.status in (401, 404):
            console.print("[red]Telegram refused the token.[/red] Check it with @BotFather.")
        else:
            console.print(f"[red]Telegram answered with an error:[/red] {escape(str(exc))}")
        return 1
    except NotSent:
        console.print(
            "[red]Could not connect to Telegram.[/red] If it is blocked where you are, set "
            '`\\[telegram] proxy = "socks5://127.0.0.1:1080"` (or http://...) in config.toml.'
        )
        return 1
    finally:
        await channel.close()
    name = escape(str(me.get("username", "?")))
    console.print(f"[green]Connected:[/green] the bot is @{name}.")
    approved = len(opened.pairing.senders("telegram"))
    if approved == 0:
        console.print(
            f"No account is approved yet: run `pa serve`, send a message to @{name}, then "
            "`pa channels approve telegram CODE`."
        )
    return 0
