"""`pa plugins`: Python plugins that are installed, which are switched on, and whether they loaded."""

from __future__ import annotations

from collections.abc import Callable

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.app import App


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    plugins_app = typer.Typer(
        help="Python plugins: extra tools from installed packages.", invoke_without_command=True
    )

    @plugins_app.callback()
    def _list(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        opened = open_app()
        try:
            report = opened.plugins
            if not report:
                console.print(
                    "[dim]No plugins installed. A package offers one through the entry point "
                    "group `personal_agent.plugins`; list its name under `\\[plugins] enabled` "
                    "in config.toml to switch it on.[/dim]"
                )
                return
            table = Table(box=None, pad_edge=False)
            for column in ("name", "state", "what it added"):
                table.add_column(column)
            colours = {"loaded": "green", "failed": "red", "missing": "red", "off": "dim"}
            for item in report:
                what = ", ".join(item.tools) if item.tools else item.detail
                colour = colours.get(item.status, "white")
                table.add_row(
                    escape(item.name), f"[{colour}]{item.status}[/{colour}]", escape(what)
                )
            console.print(table)
            console.print(
                "[dim]A plugin runs inside the agent with your rights. Switch on only code you trust; "
                "its tools ask for approval like any other, by their category.[/dim]"
            )
        finally:
            opened.close()

    return plugins_app
