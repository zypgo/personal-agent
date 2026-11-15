"""`pa outbox`: messages waiting to be delivered, or whose delivery is unclear."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import ago, opened, say

app = typer.Typer(
    help="Messages waiting to be delivered, or whose delivery is unclear.",
    invoke_without_command=True,
)


@app.callback()
def _list(
    ctx: typer.Context, status: Annotated[str | None, typer.Option("--status")] = None
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        now = agent.clock.now()
        for message in agent.outbox.list(status=status, limit=30):
            preview = escape(message.text.replace("\n", " ")[:70])
            say(
                f"#{message.id:<5} {message.status:<10} {message.channel:<9} "
                f"{message.urgency:<7} {ago(message.created_at, now):<9} {preview}"
            )


@app.command("dismiss")
def dismiss(
    message_id: Annotated[int | None, typer.Argument()] = None,
    all_: Annotated[bool, typer.Option("--all", help="Dismiss every unresolved message.")] = False,
) -> None:
    """Stop the agent worrying about messages that failed or have an unknown outcome."""
    with opened() as agent:
        ids = (
            [m.id for m in agent.outbox.unresolved()]
            if all_
            else ([message_id] if message_id else [])
        )
        say(f"Dismissed {sum(agent.outbox.cancel(i) for i in ids)} message(s).")
