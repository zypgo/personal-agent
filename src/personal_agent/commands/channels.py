"""`pa channels`: the chat channels, and who may use them.

Approving a sender happens here, at the computer, and nowhere else: a person who can only reach
the bot from a chat app cannot approve themselves, and neither can anything the bot is told.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import USAGE, console, fail, opened, say, span, table

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(help="Chat channels and who may use them.", invoke_without_command=True)

ChannelArg = Annotated[str, typer.Argument(help="telegram or dingtalk")]


@app.callback()
def _list(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        cfg = agent.config
        rows = table("channel", "state", "approved", "waiting for approval")
        rows.add_row("cli", "always available in `pa chat`", "-", "-")
        if cfg.telegram.enabled:
            variable = cfg.telegram.token_env
            state = (
                "[green]on[/green]"
                if os.environ.get(variable)
                else f"[red]on, but {escape(variable)} is not set[/red]"
            )
        else:
            state = "[dim]off[/dim] (\\[telegram] enabled = true)"
        rows.add_row(
            "telegram",
            state,
            str(len(agent.pairing.senders("telegram"))),
            str(len(agent.pairing.pending("telegram"))),
        )
        rows.add_row(
            "web",
            "[green]on[/green] (`pa web`)"
            if cfg.web.enabled
            else "[dim]off[/dim] (\\[web] enabled = true)",
            str(len(agent.web_auth.sessions())),
            str(len(agent.web_auth.pending_invites())),
        )
        missing = [
            name
            for name in (cfg.dingtalk.client_id_env, cfg.dingtalk.client_secret_env)
            if not os.environ.get(name, "").strip()
        ]
        state = "[dim]off[/dim] (\\[dingtalk] enabled = true)"
        if cfg.dingtalk.enabled:
            state = (
                "[red]on, but missing " + escape(", ".join(missing)) + "[/red]"
                if missing
                else "[green]on[/green] (configured; connection not probed)"
            )
        rows.add_row(
            "dingtalk",
            state,
            str(len(agent.pairing.senders("dingtalk"))),
            str(len(agent.pairing.pending("dingtalk"))),
        )
        for name, spec in sorted(agent.plugin_channels.items()):
            rows.add_row(
                escape(name), f"[green]on[/green] (plugin {escape(spec.plugin)})", "-", "-"
            )
        console.print(rows)
        if agent.pairing.pending():
            say("[yellow]Someone is waiting to be approved:[/yellow] `pa channels pending`")
        say(
            "Supported features: `pa channels capabilities`. This table shows configuration, not live connectivity."
        )


@app.command("capabilities")
def capabilities() -> None:
    """Show the built-in channels' features without connecting to any service."""
    from dataclasses import asdict

    from personal_agent.channels.dingtalk.channel import DingTalkChannel
    from personal_agent.channels.telegram import TelegramChannel
    from personal_agent.channels.web.channel import WebChannel

    labels = {
        "text": "text",
        "markdown": "Markdown",
        "images_in": "images in",
        "audio_in": "voice in",
        "documents_out": "files out",
        "proactive": "push",
        "approval_buttons": "approval buttons",
        "streaming": "live answer",
    }
    with opened() as agent:
        cfg = agent.config
        rows = table("channel", "enabled", "supported features")
        rows.add_row("cli", "yes", "text, local commands")
        for channel in (TelegramChannel, WebChannel, DingTalkChannel):
            channel_cfg = getattr(cfg, channel.name)
            flags = asdict(channel.capabilities)
            flags["images_in"] &= cfg.media.images
            flags["audio_in"] &= cfg.media.voice
            flags["documents_out"] &= cfg.artifacts.enabled
            if channel.name == "web":
                flags["streaming"] &= cfg.web.stream_responses
            rows.add_row(
                channel.name,
                "yes" if channel_cfg.enabled else "no",
                ", ".join(labels[key] for key, enabled in flags.items() if enabled),
            )
        for name in sorted(agent.plugin_channels):
            rows.add_row(escape(name), "yes", "plugin-defined; metadata not inspected")
        console.print(rows)
        say("A supported feature still needs valid credentials, pairing and platform permissions.")


@app.command("pending")
def pending() -> None:
    """People who messaged a bot and are waiting for you to approve them."""
    with opened() as agent:
        found = agent.pairing.pending()
        if not found:
            say("[dim]Nobody is waiting.[/dim]")
            return
        now = agent.clock.now()
        rows = table("code", "channel", "who", "asked", "expires in")
        for item in found:
            rows.add_row(
                f"[bold]{escape(item.code)}[/bold]",
                escape(item.channel),
                escape(item.label or item.sender),
                f"{span(now - item.created_at)} ago",
                span(item.expires_at - now),
            )
        console.print(rows)
        say("[dim]Only approve a code you saw appear on your own phone.[/dim]")


@app.command("approve")
def approve(
    channel: ChannelArg,
    code: Annotated[str, typer.Argument(help="The code the bot sent, such as ABCD-EFGH.")],
) -> None:
    """Let the person behind a pairing code talk to the agent."""
    with opened() as agent:
        request = agent.pairing.peek(code)
        if request is None:
            fail(
                "No such code",
                "(it may have expired: message the bot again). "
                "`pa channels pending` lists the live ones.",
            )
        if request.channel != channel.lower():
            fail(f"That code belongs to {request.channel}, not {channel}.")
        sender = agent.pairing.approve(code)
        if sender is None:  # it expired in the moment between looking and approving
            fail("That code just expired.", "Ask them to message the bot again.")
        say(
            f"[green]Approved {escape(sender.label or sender.sender)} on {escape(sender.channel)}.[/green]"
        )


@app.command("deny")
def deny(code: Annotated[str, typer.Argument(help="The code to throw away.")]) -> None:
    """Refuse a pairing request."""
    with opened() as agent:
        if agent.pairing.deny(code) is None:
            fail("No such code.")
        say("Refused.")


@app.command("allow")
def allow(
    channel: ChannelArg,
    sender: Annotated[
        str,
        typer.Argument(
            help="The full account id from `pa channels senders`; prefer pairing codes for DingTalk."
        ),
    ],
    label: Annotated[
        str, typer.Option("--label", "-l", help="A name for your own reference.")
    ] = "",
) -> None:
    """Approve an account whose id you already know, without a code."""
    with opened() as agent:
        agent.pairing.allow(channel.lower(), sender, label)
        say(f"[green]Approved {escape(sender)} on {escape(channel)}.[/green]")


@app.command("senders")
def senders() -> None:
    """Accounts that may talk to the agent."""
    with opened() as agent:
        found = agent.pairing.senders()
        if not found:
            say("[dim]No account is approved on any chat channel.[/dim]")
            return
        now = agent.clock.now()
        rows = table("channel", "id", "name", "approved", "last heard")
        for item in found:
            rows.add_row(
                escape(item.channel),
                escape(item.sender),
                escape(item.label),
                f"{span(now - item.approved_at)} ago",
                f"{span(now - item.last_seen_at)} ago" if item.last_seen_at else "-",
            )
        console.print(rows)


@app.command("revoke")
def revoke(
    channel: ChannelArg,
    sender: Annotated[str, typer.Argument(help="The account id from `pa channels senders`.")],
) -> None:
    """Stop an account from talking to the agent."""
    with opened() as agent:
        if not agent.pairing.revoke(channel.lower(), sender):
            fail("That account was not approved.")
        say("Revoked.")


@app.command("test")
def test(
    channel: ChannelArg = "telegram",
) -> None:
    """Probe Telegram identity or DingTalk gateway authentication; never send a chat message."""
    if channel.lower() not in {"telegram", "dingtalk"}:
        fail(f"No test for {channel}.", code=USAGE)
    with opened() as agent:
        probe = _test_dingtalk if channel.lower() == "dingtalk" else _test_telegram
        raise typer.Exit(asyncio.run(probe(agent)))


async def _test_dingtalk(agent: App) -> int:
    from personal_agent.channels.base import ChannelError
    from personal_agent.channels.dingtalk.channel import DingTalkChannel

    channel = DingTalkChannel(agent)
    try:
        await channel.api.open_connection()
    except ChannelError as exc:
        say(f"[red]{escape(str(exc))}[/red] See docs/DINGTALK.md.")
        return 1
    finally:
        await channel.close()
    say(
        "[green]DingTalk gateway authentication passed.[/green] "
        "This does not prove the daemon is connected or the bot has permission to send."
    )
    if not agent.pairing.senders("dingtalk"):
        say("Start `pa serve`, message the private bot, then `pa channels approve dingtalk CODE`.")
    return 0


async def _test_telegram(agent: App) -> int:
    from personal_agent.channels.base import ChannelError
    from personal_agent.channels.telegram import TelegramChannel
    from personal_agent.channels.telegram.api import NotSent, TelegramApiError

    cfg = agent.config.telegram
    channel = TelegramChannel(agent)
    try:
        me = await channel.api.get_me()
    except ChannelError as exc:
        say(f"[red]{escape(str(exc))}[/red]")
        say(f"Put {escape(cfg.token_env)}=... in {escape(str(agent.paths.env_file))}")
        return 1
    except TelegramApiError as exc:
        if exc.status in (401, 404):
            say("[red]Telegram refused the token.[/red] Check it with @BotFather.")
        else:
            say(f"[red]Telegram answered with an error:[/red] {escape(str(exc))}")
        return 1
    except NotSent:
        say(
            "[red]Could not connect to Telegram.[/red] If it is blocked where you are, set "
            '`\\[telegram] proxy = "socks5://127.0.0.1:1080"` (or http://...) in config.toml.'
        )
        return 1
    finally:
        await channel.close()
    name = escape(str(me.get("username", "?")))
    say(f"[green]Connected:[/green] the bot is @{name}.")
    if not agent.pairing.senders("telegram"):
        say(
            f"No account is approved yet: run `pa serve`, send a message to @{name}, then "
            "`pa channels approve telegram CODE`."
        )
    return 0
