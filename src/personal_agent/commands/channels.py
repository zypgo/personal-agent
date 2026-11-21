"""`pa channels`: the chat channels, and who may use them.

Approving a sender happens here, at the computer, and nowhere else: a person who can only reach
the bot from a chat app cannot approve themselves, and neither can anything the bot is told.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import USAGE, console, fail, opened, say, span, table

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(help="Chat channels and who may use them.", invoke_without_command=True)

ChannelArg = Annotated[str, typer.Argument(help="telegram or dingtalk")]


def _owner_binding_scope(agent: App, channel: str, recipient: str) -> tuple[str, str]:
    """Resolve trusted local configuration; a caller never chooses another bot's account scope."""
    if channel == "telegram":
        token = os.environ.get(agent.config.telegram.token_env, "").strip()
        account, separator, _ = token.partition(":")
        if not separator or not account.isdigit() or not recipient.isdigit():
            fail(
                "Telegram identity is not configured or the account ID is invalid.",
                "Set the bot token, or use the workspace Devices page and send /bind CODE to the bot.",
            )
        return account, recipient
    if channel == "dingtalk":
        from personal_agent.channels.dingtalk.channel import account_key, recipient_key

        try:
            parts = json.loads(recipient)
        except (TypeError, ValueError):
            parts = None
        client = os.environ.get(agent.config.dingtalk.client_id_env, "").strip()
        if (
            not isinstance(parts, list)
            or len(parts) != 3
            or not all(isinstance(part, str) and part and len(part) <= 200 for part in parts)
            or not client
            or parts[0] != client
            or (
                agent.config.dingtalk.corp_id is not None
                and parts[1] != agent.config.dingtalk.corp_id
            )
            or recipient_key(*parts) != recipient
        ):
            fail(
                "That DingTalk identity does not match the configured app and organization.",
                "Use the workspace Devices page and send /bind CODE in the robot's private chat.",
            )
        return account_key(parts[0], parts[1]), parts[2]
    fail("Owner binding supports telegram or dingtalk.", code=USAGE)


def _bind_local_owner(agent: App, channel: str, recipient: str, scope: tuple[str, str]) -> None:
    from personal_agent.collaboration.models import OWNER_ID, OWNER_SPACE

    if agent.identities is not None:
        invitation = agent.identities.create_channel_binding(OWNER_ID, OWNER_SPACE)
        agent.identities.bind_channel(invitation.code, channel, scope[0], scope[1], recipient)


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
    """Bind your own account to the owner's private space. Invite collaborators in the workspace."""
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
        scope = _owner_binding_scope(agent, request.channel, request.sender)
        with agent.db.tx():
            sender = agent.pairing.approve(code)
            if sender is None:  # it expired in the moment between looking and approving
                fail("That code just expired.", "Ask them to message the bot again.")
            _bind_local_owner(agent, sender.channel, sender.sender, scope)
        say(
            f"[green]Approved {escape(sender.label or sender.sender)} on {escape(sender.channel)}.[/green]"
        )
        say("Bound to the owner's private space. Use a project invitation for a collaborator.")


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
    """Bind your own known account to the owner's private space, without a code."""
    with opened() as agent:
        scope = _owner_binding_scope(agent, channel.lower(), sender)
        with agent.db.tx():
            agent.pairing.allow(channel.lower(), sender, label)
            _bind_local_owner(agent, channel.lower(), sender, scope)
        say(f"[green]Approved {escape(sender)} on {escape(channel)}.[/green]")
        say("Bound to the owner's private space. Use a project invitation for a collaborator.")


@app.command("senders")
def senders() -> None:
    """Explicit identity bindings and legacy channel approvals."""
    with opened() as agent:
        found = agent.pairing.senders()
        bindings = agent.db.query(
            "SELECT c.*, p.display_name, s.name AS space_name FROM channel_identities c "
            "JOIN principals p ON p.id = c.principal_id JOIN spaces s ON s.id = c.space_id "
            "WHERE c.revoked_at IS NULL ORDER BY c.channel, c.bound_at, c.sender_id"
        )
        if not found and not bindings:
            say("[dim]No account is approved on any chat channel.[/dim]")
            return
        rows = table("channel", "id", "name", "space", "access")
        legacy_labels = {(item.channel, item.sender): item.label for item in found}
        represented = set()
        for binding in bindings:
            key = binding["channel"], binding["recipient_id"]
            represented.add(key)
            active = (
                agent.identities is not None
                and agent.identities.lookup_channel(
                    binding["channel"], binding["account_id"], binding["sender_id"]
                )
                is not None
            )
            label = legacy_labels.get(key) or binding["display_name"]
            rows.add_row(
                escape(binding["channel"]),
                escape(binding["recipient_id"]),
                escape(label),
                escape(binding["space_name"]),
                "bound" if active else "rebind required",
            )
        for item in found:
            if (item.channel, item.sender) in represented:
                continue
            legacy = agent.identities is not None and agent.identities.legacy_owner_sender(
                item.channel, item.sender
            )
            # Explicit revocation suppresses the pre-V3 owner fallback as well.
            explicit = agent.db.one(
                "SELECT 1 FROM channel_identities WHERE channel = ? AND recipient_id = ?",
                (item.channel, item.sender),
            )
            rows.add_row(
                escape(item.channel),
                escape(item.sender),
                escape(item.label),
                "owner personal" if legacy and explicit is None else "-",
                "legacy owner" if legacy and explicit is None else "binding required",
            )
        console.print(rows)


@app.command("revoke")
def revoke(
    channel: ChannelArg,
    sender: Annotated[str, typer.Argument(help="The account id from `pa channels senders`.")],
) -> None:
    """Stop an account from talking to the agent."""
    with opened() as agent:
        changed = False
        with agent.db.tx():
            if agent.identities is not None:
                for row in agent.db.query(
                    "SELECT account_id, sender_id FROM channel_identities WHERE channel=? AND recipient_id=? AND revoked_at IS NULL",
                    (channel.lower(), sender),
                ):
                    changed |= agent.identities.revoke_channel(
                        "owner",
                        channel.lower(),
                        row["account_id"],
                        row["sender_id"],
                    )
            changed |= agent.pairing.revoke(channel.lower(), sender)
        if not changed:
            fail("That account was not approved.")
        say("Revoked.")


@app.command("test")
def test(
    channel: ChannelArg = "telegram",
) -> None:
    """Check the Telegram bot identity or the DingTalk gateway login."""
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
