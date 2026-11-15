"""`pa mcp`: the MCP servers the agent may use, and the review their tools need first.

A tool's description is text the model believes. So no tool of an MCP server is offered until the
owner has read its definition here (`pa mcp trust`), and a tool that is new or whose definition
changed later waits for another look. Nothing in this module lets a server's text reach the
terminal unescaped: control codes and hidden characters were already removed from it.
"""

from __future__ import annotations

import asyncio
import difflib
from collections.abc import Callable
from typing import Annotated

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from personal_agent.app import App
from personal_agent.config import env_references
from personal_agent.plugins.mcp import ServerReport, ToolInfo
from personal_agent.plugins.mcp_config import ConfigEditError, add_server, remove_server
from personal_agent.plugins.mcp_convert import model_view, public_name
from personal_agent.plugins.mcp_pins import Pin, Review
from personal_agent.security.sanitize import hides_text
from personal_agent.tools.base import Category

NAME_HELP = "The server's name, as in `pa mcp list`."


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    mcp_app = typer.Typer(
        help="MCP servers: programs and web services that give the agent more tools.",
        invoke_without_command=True,
    )

    def look(opened: App, name: str) -> ServerReport:
        """Connect to one server, read what it offers, and disconnect again."""
        if name not in opened.mcp.names():
            console.print(f"[red]No MCP server called {escape(name)}.[/red] See `pa mcp list`.")
            raise typer.Exit(1)

        async def go() -> ServerReport:
            try:
                return await opened.mcp.inspect(name)
            finally:
                await opened.stop_services()

        console.print(f"Connecting to {escape(name)} ...")
        return asyncio.run(go())

    def failed(report: ServerReport) -> None:
        console.print(f"[red]Could not use {escape(report.name)}:[/red] {escape(report.error)}")
        if report.log_tail:
            console.print("[dim]What the program printed on its way out:[/dim]")
            console.print(escape(report.log_tail[-1_500:]))
        raise typer.Exit(1)

    def kind_of(info: ToolInfo) -> str:
        return "read" if info.category is Category.READ else "action"

    # ---- list ---------------------------------------------------------------------------------
    def list_servers() -> None:
        opened = open_app()
        try:
            summaries = opened.mcp.summaries()
            if not summaries:
                console.print(
                    "[dim]No MCP servers yet. Add one with "
                    "`pa mcp add NAME -- COMMAND ARGS...` or `pa mcp add NAME --url https://...`.[/dim]"
                )
                return
            table = Table(box=None, pad_edge=False)
            for column in ("name", "kind", "runs", "state", "reviewed"):
                table.add_column(column)
            for item in summaries:
                if item.reviewed_tools:
                    reviewed = f"{item.reviewed_tools} tool(s)"
                elif item.pin == "auto":
                    reviewed = "accepted on first connection"
                else:
                    reviewed = f"[yellow]no: run `pa mcp trust {escape(item.name)}`[/yellow]"
                table.add_row(
                    escape(item.name),
                    item.transport,
                    escape(item.target if len(item.target) <= 60 else item.target[:59] + "…"),
                    "on" if item.enabled else "off",
                    reviewed,
                )
            console.print(table)
        finally:
            opened.close()

    @mcp_app.callback()
    def _default(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            list_servers()

    @mcp_app.command("list")
    def list_() -> None:
        """The configured servers and whether their tools have been reviewed (does not connect)."""
        list_servers()

    # ---- tools --------------------------------------------------------------------------------
    @mcp_app.command("tools")
    def tools(name: str = typer.Argument(..., help=NAME_HELP)) -> None:
        """Connect to a server and list its tools and what will happen to each."""
        opened = open_app()
        try:
            report = look(opened, name)
            if report.status != "connected":
                failed(report)
            if not report.tools:
                console.print(f"{escape(name)} is working but offers no tools.")
                return
            table = Table(box=None, pad_edge=False)
            for column in ("tool (as the model sees it)", "kind", "state"):
                table.add_column(column)
            waiting = 0
            for info in report.tools:
                shown = public_name(name, info.name, set())
                if info.filtered:
                    state = f"[dim]left out: {escape(info.filtered)}[/dim]"
                elif info.offered:
                    state = "[green]offered[/green]"
                else:
                    waiting += 1
                    state = f"[yellow]withheld: {escape(info.withheld)}[/yellow]"
                table.add_row(escape(shown), kind_of(info), state)
            console.print(table)
            console.print(
                "[dim]read: runs without asking. action: asks you first, each time, "
                "unless a rule allows it.[/dim]"
            )
            if waiting:
                console.print(f"Read {waiting} definition(s) before use: `pa mcp trust {name}`.")
        finally:
            opened.close()

    # ---- trust --------------------------------------------------------------------------------
    def show_definition(info: ToolInfo, before: Pin | None) -> None:
        label, colour = {
            Review.UNREVIEWED: ("not reviewed yet", "yellow"),
            Review.NEW: ("new", "yellow"),
            Review.CHANGED: ("CHANGED since you reviewed it", "red"),
        }[info.review]
        kind = (
            "read: runs without asking"
            if info.category is Category.READ
            else "action: asks you each time"
        )
        console.print(f"\n[bold]{escape(info.name)}[/bold]  [{colour}]{label}[/{colour}]  ({kind})")
        now = model_view(info.definition)
        if before is not None and info.review is Review.CHANGED:
            then = model_view(before.definition)
            changes = [
                line
                for line in difflib.unified_diff(then, now, lineterm="", n=2)
                if not line.startswith(("---", "+++", "@@"))
            ]
            if not changes:
                console.print(
                    "  The text the model reads is the same. Something it does not show changed."
                )
            for line in changes:
                style = {"+": "green", "-": "red"}.get(line[:1])
                text = escape(line)
                console.print(f"  [{style}]{text}[/{style}]" if style else f"  [dim]{text}[/dim]")
        else:
            for line in now:
                console.print(f"  {escape(line)}")
        if hides_text(
            " ".join(
                str(info.definition.get(key) or "") for key in ("name", "title", "description")
            )
            + str(info.definition.get("inputSchema"))
        ):
            console.print(
                "  [yellow]This definition contained hidden characters (invisible text or "
                "text-direction codes). They were removed from what is shown here and from what "
                "the model reads.[/yellow]"
            )

    @mcp_app.command("trust")
    def trust(
        name: str = typer.Argument(..., help=NAME_HELP),
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
        ] = False,
    ) -> None:
        """Read what a server's tools say about themselves, and accept them."""
        opened = open_app()
        try:
            report = look(opened, name)
            if report.status != "connected":
                failed(report)
            reviewable = [t for t in report.tools if not t.filtered]
            if not reviewable:
                console.print(
                    f"{escape(name)} offers no tools to review (after your allow_tools/deny_tools)."
                )
                raise typer.Exit(1)
            pending = [t for t in reviewable if t.review is not Review.TRUSTED]
            if not pending:
                console.print(
                    f"[green]Nothing new:[/green] all {len(reviewable)} tool(s) of {escape(name)} "
                    "are as you reviewed them."
                )
                return
            before = opened.mcp.pins.for_server(name)
            console.print(
                f"[bold]{escape(name)}[/bold] offers {len(reviewable)} tool(s); "
                f"{len(pending)} need your review."
            )
            for info in pending:
                show_definition(info, before.get(info.name))
            gone = sorted(set(before) - {t.name for t in reviewable})
            if gone:
                console.print(
                    f"\n{len(gone)} tool(s) you reviewed before are no longer offered: "
                    + escape(", ".join(gone))
                )
            console.print(
                "\nThe model reads these descriptions as if they were instructions. Accept them "
                "only if you trust whoever runs the server."
            )
            if not yes and not typer.confirm("Trust these definitions?", default=False):
                console.print("Nothing was changed.")
                raise typer.Exit(1)
            count = opened.mcp.pin(name, report)
            console.print(
                f"[green]Reviewed {count} tool(s).[/green] A running agent picks this up on its "
                "next run; any change to a definition later will ask you again."
            )
        finally:
            opened.close()

    # ---- add / remove ---------------------------------------------------------------------------
    @mcp_app.command("add")
    def add(
        name: Annotated[str, typer.Argument(help="A short name: letters, digits, hyphens.")],
        command: Annotated[
            list[str] | None,
            typer.Argument(help="For a program: the command and its arguments, after `--`."),
        ] = None,
        url: Annotated[
            str | None, typer.Option("--url", help="For a web service: its address.")
        ] = None,
        sse: Annotated[
            bool, typer.Option("--sse", help="The address is an older SSE endpoint.")
        ] = False,
        env: Annotated[
            list[str] | None,
            typer.Option(
                "--env",
                "-e",
                help="KEY=VALUE for the program, or just KEY to pass the value of KEY from .env.",
            ),
        ] = None,
        header: Annotated[
            list[str] | None,
            typer.Option(
                "--header",
                "-H",
                help="Name=Value; write secrets as '${NAME}' (single quotes) and keep them in .env.",
            ),
        ] = None,
        read_only: Annotated[
            list[str] | None,
            typer.Option("--read-only", help="Tools that only look at things, e.g. 'get_*'."),
        ] = None,
        auto: Annotated[
            bool,
            typer.Option(
                "--accept-first-listing", help="Trust the tools it lists on first connection."
            ),
        ] = False,
    ) -> None:
        """Add a server to config.toml. Its tools stay off until you review them."""
        entry: dict = {"name": name}
        if url:
            if command:
                console.print("[red]Give either --url or a command, not both.[/red]")
                raise typer.Exit(2)
            entry.update(transport="sse" if sse else "http", url=url)
        elif command:
            entry.update(command=command[0], args=command[1:])
        else:
            console.print("[red]Give a command after `--`, or an address with --url.[/red]")
            raise typer.Exit(2)

        values: dict[str, str] = {}
        for item in env or []:
            key, has_value, value = item.partition("=")
            values[key] = value if has_value else "${" + key + "}"
        if values:
            entry["env"] = values
        headers: dict[str, str] = {}
        for item in header or []:
            key, has_value, value = item.partition("=")
            if not has_value:
                console.print(f"[red]A header needs a value: {escape(item)} (Name=Value).[/red]")
                raise typer.Exit(2)
            headers[key.strip()] = value
        if headers:
            entry["headers"] = headers
        if read_only:
            entry["read_only"] = list(read_only)
        if auto:
            entry["pin"] = "auto"

        opened = open_app()
        try:
            try:
                add_server(opened.paths.config_file, entry)
            except ConfigEditError as exc:
                console.print(f"[red]Not added:[/red] {escape(str(exc))}")
                raise typer.Exit(1) from exc
            console.print(
                f"[green]Added[/green] {escape(name)} to {escape(str(opened.paths.config_file))}"
            )
            referenced = sorted(
                {
                    ref
                    for text in (*values.values(), *headers.values())
                    for ref in env_references(text)
                }
            )
            if referenced:
                console.print(
                    f"It reads {', '.join(referenced)} from {escape(str(opened.paths.env_file))}; "
                    "put the value(s) there."
                )
            console.print(
                f"Next: `pa mcp tools {name}` shows what it offers, "
                f"`pa mcp trust {name}` reviews it and switches its tools on."
            )
        finally:
            opened.close()

    @mcp_app.command("remove")
    def remove(
        name: str = typer.Argument(..., help=NAME_HELP),
        yes: Annotated[
            bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")
        ] = False,
    ) -> None:
        """Take a server out of config.toml and forget what you reviewed of it."""
        opened = open_app()
        try:
            if name not in opened.mcp.names():
                console.print(f"[red]No MCP server called {escape(name)}.[/red]")
                raise typer.Exit(1)
            if not yes and not typer.confirm(f"Remove the MCP server {name}?", default=False):
                console.print("Kept.")
                raise typer.Exit(1)
            try:
                removed = remove_server(opened.paths.config_file, name)
            except ConfigEditError as exc:
                console.print(f"[red]Not removed:[/red] {escape(str(exc))}")
                raise typer.Exit(1) from exc
            if not removed:
                console.print(f"{escape(name)} is not in {escape(str(opened.paths.config_file))}.")
                raise typer.Exit(1)
            opened.mcp.pins.forget(name)
            console.print(f"Removed {escape(name)}. A running agent stops using it when restarted.")
        finally:
            opened.close()

    return mcp_app
