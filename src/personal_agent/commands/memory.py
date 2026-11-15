"""`pa memory` and `pa prefs`: the agent's memory, and the instructions only you can change."""

from __future__ import annotations

from typing import Annotated

import click
import typer
from rich.markup import escape

from personal_agent.clock import format_local
from personal_agent.commands.common import echo, opened, say
from personal_agent.memory.store import INSTRUCTIONS_PATH, PROFILE_PATH, PROPOSALS_PATH, Actor

app = typer.Typer(help="Inspect the agent's memory and its change history.", no_args_is_help=True)
prefs = typer.Typer(
    help="Your standing instructions (the agent can read but not change them).",
    no_args_is_help=True,
)


@app.command("view")
def view(path: Annotated[str, typer.Argument()] = "/memories") -> None:
    """Show a memory directory or file."""
    with opened() as agent:
        echo(agent.memory.view(path))


@app.command("history")
def history(
    path: Annotated[str, typer.Argument()],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
) -> None:
    """Who changed a file, when, and how."""
    with opened() as agent:
        for row in agent.memory.history(path, limit):
            say(
                f"version {row['id']:<5} {row['action']:<8} by {escape(row['actor']):<6} "
                f"{format_local(row['created_at'], agent.tz)}"
            )


@app.command("restore")
def restore(version_id: Annotated[int, typer.Argument()]) -> None:
    """Undo one change: put the file back as it was before that version."""
    with opened() as agent:
        echo(agent.memory.restore(version_id))


@prefs.command("show")
def show() -> None:
    """Print your standing instructions and profile."""
    with opened() as agent:
        for title, path in (("instructions", INSTRUCTIONS_PATH), ("profile", PROFILE_PATH)):
            say(f"[bold]{title}[/bold] ({path})")
            text = agent.memory.read_text(path)
            say(escape(text) if text else "[dim](empty)[/dim]")


@prefs.command("edit")
def edit(
    profile: Annotated[
        bool, typer.Option("--profile", help="Edit profile.md instead of instructions.md.")
    ] = False,
) -> None:
    """Edit your standing instructions in $EDITOR. Takes effect on the agent's next run."""
    with opened() as agent:
        path = PROFILE_PATH if profile else INSTRUCTIONS_PATH
        edited = click.edit(agent.memory.read_text(path) or "", extension=".md")
        if edited is None:
            say("No changes.")
            return
        agent.memory.write_text(path, edited, actor=Actor.USER)
        say(f"Saved {path}.")


@prefs.command("proposals")
def proposals() -> None:
    """Changes the agent suggests for your preferences (it cannot make them itself)."""
    with opened() as agent:
        text = agent.memory.read_text(PROPOSALS_PATH)
        say(escape(text) if text else "[dim](none)[/dim]")
