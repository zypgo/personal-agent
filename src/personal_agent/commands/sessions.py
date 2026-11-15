"""`pa sessions`: the conversations the agent keeps."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.markup import escape

from personal_agent.commands.common import ago, opened, say
from personal_agent.core.sessions import DEFAULT_SESSION

app = typer.Typer(help="List and clear conversations.", invoke_without_command=True)


@app.callback()
def _list(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        now = agent.clock.now()
        for info in agent.sessions.list_sessions():
            say(
                f"{escape(info.session_key):<30} {info.items:>5} items  "
                f"{ago(info.last_activity, now)}"
            )


@app.command("clear")
def clear(key: Annotated[str, typer.Argument()] = DEFAULT_SESSION) -> None:
    """Hide a conversation from the model (the log keeps it)."""
    with opened() as agent:
        agent.sessions.clear(key)
        say(f"Cleared session '{escape(key)}'.")
