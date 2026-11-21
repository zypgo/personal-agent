"""`pa`: the command line.

This module only puts it together. Every command lives in `personal_agent/commands/`, one Typer
app per topic; `common.py` there holds what they share (the terminal, opening the agent's home,
saying that something failed). The order below is the order `pa --help` lists them in.
"""

from __future__ import annotations

import typer

from personal_agent import __version__
from personal_agent.commands import (
    agents,
    budget,
    channels,
    chat,
    db,
    dream,
    evals,
    inspection,
    mcp,
    memory,
    node,
    outbox,
    plugins,
    serve,
    service,
    sessions,
    setup,
    skills,
    timers,
    vault,
    web,
    webhooks,
)

app = typer.Typer(
    name="pa",
    help="Qixu 栖序: a personal agent that stays on your computer.",
    no_args_is_help=True,
    add_completion=False,
)


def _version(value: bool) -> None:
    if value:
        typer.echo(f"Qixu {__version__} (personal-agent)")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Show version."
    ),
) -> None:
    """Qixu."""


# Commands that stand alone (a Typer added without a name contributes its commands directly).
for alone in (setup.app, chat.app, serve.app, inspection.app):
    app.add_typer(alone)

# One group per topic: `pa timers add ...`, `pa skills install ...`.
GROUPS = {
    "sessions": sessions.app,
    "outbox": outbox.app,
    "timers": timers.app,
    "budget": budget.app,
    "db": db.app,
    "memory": memory.app,
    "prefs": memory.prefs,
    "vault": vault.app,
    "skills": skills.app,
    "eval": evals.app,
    "mcp": mcp.app,
    "plugins": plugins.app,
    "channels": channels.app,
    "web": web.app,
    "node": node.app,
    "agents": agents.app,
    "dream": dream.app,
    "service": service.app,
    "webhooks": webhooks.app,
}
for name, group in GROUPS.items():
    app.add_typer(group, name=name)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
