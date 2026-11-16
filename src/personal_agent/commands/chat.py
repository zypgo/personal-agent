"""`pa ask`, `pa chat` and the approval commands: talking to the agent and answering it.

The work is in `conversation.py`; it imports the agent's runtime, which takes most of a second to
load, so it is imported here inside each command and `pa --help` stays fast.
"""

from __future__ import annotations

from typing import Annotated

import typer

from personal_agent.commands import views
from personal_agent.commands.common import FAILED, opened
from personal_agent.core.sessions import DEFAULT_SESSION

app = typer.Typer()

SessionOption = Annotated[str, typer.Option("--session", "-s", help="Conversation to continue.")]
Requests = Annotated[
    list[str] | None, typer.Argument(help="Request numbers from `pa approvals`, or `all`.")
]


@app.command()
def ask(
    text: Annotated[str, typer.Argument(help="What to ask.")],
    session: SessionOption = DEFAULT_SESSION,
) -> None:
    """Ask once and print the answer."""
    from personal_agent.commands.conversation import ask_once

    with opened() as agent:
        if not ask_once(agent, text, session):
            raise typer.Exit(FAILED)


@app.command()
def chat(session: SessionOption = DEFAULT_SESSION) -> None:
    """Talk to the agent."""
    from personal_agent.commands.conversation import chat_loop, enable_history

    with opened() as agent:
        enable_history(agent)
        chat_loop(agent, session)


@app.command()
def approvals(
    all_: Annotated[bool, typer.Option("--all", "-a", help="Also show answered requests.")] = False,
) -> None:
    """Actions the agent is waiting for your yes or no on."""
    with opened() as agent:
        views.show_approvals(agent, include_decided=all_)


@app.command()
def approve(
    ids: Requests = None,
    note: Annotated[str | None, typer.Option("--note", "-m", help="A word for the agent.")] = None,
) -> None:
    """Allow a waiting action; the paused work continues."""
    _answer(True, ids or [], note)


@app.command()
def deny(
    ids: Requests = None,
    note: Annotated[
        str | None, typer.Option("--note", "-m", help="Why, so the agent can adapt.")
    ] = None,
) -> None:
    """Refuse a waiting action; the agent is told and carries on without it."""
    _answer(False, ids or [], note)


def _answer(approve: bool, words: list[str], note: str | None) -> None:
    from personal_agent.commands.conversation import decide

    with opened() as agent:
        if not decide(agent, approve, words, note):
            raise typer.Exit(1)
