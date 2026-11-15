"""`pa plugins`: Python plugins that are installed, which are switched on, and whether they loaded."""

from __future__ import annotations

import typer
from rich.markup import escape

from personal_agent.commands.common import console, opened, say, table

app = typer.Typer(
    help="Python plugins: extra tools from installed packages.", invoke_without_command=True
)

COLOURS = {"loaded": "green", "failed": "red", "missing": "red", "off": "dim"}


@app.callback()
def _list(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        if not agent.plugins:
            say(
                "[dim]No plugins installed. A package offers one through the entry point "
                "group `personal_agent.plugins`; list its name under `\\[plugins] enabled` "
                "in config.toml to switch it on.[/dim]"
            )
            return
        rows = table("name", "state", "what it added")
        for item in agent.plugins:
            what = ", ".join(item.tools) if item.tools else item.detail
            colour = COLOURS.get(item.status, "white")
            rows.add_row(escape(item.name), f"[{colour}]{item.status}[/{colour}]", escape(what))
        console.print(rows)
        say(
            "[dim]A plugin runs inside the agent with your rights. Switch on only code you trust; "
            "its tools ask for approval like any other, by their category.[/dim]"
        )
