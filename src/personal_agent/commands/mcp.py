"""`pa mcp`: the MCP servers the agent may use, and the review their tools need first.

A tool's description is text the model believes. So no tool of an MCP server is offered until the
owner has read its definition here (`pa mcp trust`), and a tool that is new or whose definition
changed later waits for another look. Nothing in this module lets a server's text reach the
terminal unescaped: control codes and hidden characters were already removed from it.
"""

from __future__ import annotations

import asyncio
import difflib
from typing import TYPE_CHECKING, Annotated, Any

import typer
from rich.markup import escape

from personal_agent.commands.common import USAGE, console, fail, opened, say, table
from personal_agent.config import env_references
from personal_agent.plugins.mcp_config import ConfigEditError, add_server, remove_server
from personal_agent.plugins.mcp_convert import model_view, public_name
from personal_agent.plugins.mcp_pins import Pin, Review
from personal_agent.security.sanitize import hides_text

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.plugins.mcp import ServerReport, ToolInfo

app = typer.Typer(
    help="MCP servers: programs and web services that give the agent more tools.",
    invoke_without_command=True,
)

NameArg = Annotated[str, typer.Argument(help="The server's name, as in `pa mcp list`.")]
Yes = Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")]


def _look(agent: App, name: str) -> ServerReport:
    """Connect to one server, read what it offers, and disconnect again. Ends the command if the
    server is unknown or cannot be used."""
    if name not in agent.mcp.names():
        fail(f"No MCP server called {name}.", "See `pa mcp list`.")

    async def go() -> ServerReport:
        try:
            return await agent.mcp.inspect(name)
        finally:
            await agent.stop_services()

    say(f"Connecting to {escape(name)} ...")
    report = asyncio.run(go())
    if report.status != "connected":
        say(f"[red]Could not use {escape(report.name)}:[/red] {escape(report.error)}")
        if report.log_tail:
            say("[dim]What the program printed on its way out:[/dim]")
            say(escape(report.log_tail[-1_500:]))
        raise typer.Exit(1)
    return report


def _kind(info: ToolInfo) -> str:
    return "read" if info.reads_only else "action"


# ---- list ---------------------------------------------------------------------------------
def _list_servers() -> None:
    with opened() as agent:
        summaries = agent.mcp.summaries()
        if not summaries:
            say(
                "[dim]No MCP servers yet. Add one with `pa mcp add NAME -- COMMAND ARGS...` "
                "or `pa mcp add NAME --url https://...`.[/dim]"
            )
            return
        rows = table("name", "kind", "runs", "state", "reviewed")
        for item in summaries:
            if item.reviewed_tools:
                reviewed = f"{item.reviewed_tools} tool(s)"
            elif item.pin == "auto":
                reviewed = "accepted on first connection"
            else:
                reviewed = f"[yellow]no: run `pa mcp trust {escape(item.name)}`[/yellow]"
            rows.add_row(
                escape(item.name),
                item.transport,
                escape(item.target if len(item.target) <= 60 else item.target[:59] + "…"),
                "on" if item.enabled else "off",
                reviewed,
            )
        console.print(rows)


@app.callback()
def _default(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        _list_servers()


@app.command("list")
def list_() -> None:
    """The configured servers and whether their tools have been reviewed (does not connect)."""
    _list_servers()


# ---- tools --------------------------------------------------------------------------------
@app.command("tools")
def tools(name: NameArg) -> None:
    """Connect to a server and list its tools and what will happen to each."""
    with opened() as agent:
        report = _look(agent, name)
        if not report.tools:
            say(f"{escape(name)} is working but offers no tools.")
            return
        rows = table("tool (as the model sees it)", "kind", "state")
        waiting = 0
        for info in report.tools:
            if info.filtered:
                state = f"[dim]left out: {escape(info.filtered)}[/dim]"
            elif info.offered:
                state = "[green]offered[/green]"
            else:
                waiting += 1
                state = f"[yellow]withheld: {escape(info.withheld)}[/yellow]"
            rows.add_row(escape(public_name(name, info.name, set())), _kind(info), state)
        console.print(rows)
        say(
            "[dim]read: runs without asking. action: asks you first, each time, "
            "unless a rule allows it.[/dim]"
        )
        if waiting:
            say(f"Read {waiting} definition(s) before use: `pa mcp trust {escape(name)}`.")


# ---- trust --------------------------------------------------------------------------------
def _show_definition(info: ToolInfo, before: Pin | None) -> None:
    label, colour = {
        Review.UNREVIEWED: ("not reviewed yet", "yellow"),
        Review.NEW: ("new", "yellow"),
        Review.CHANGED: ("CHANGED since you reviewed it", "red"),
    }[info.review]
    kind = "read: runs without asking" if info.reads_only else "action: asks you each time"
    say(f"\n[bold]{escape(info.name)}[/bold]  [{colour}]{label}[/{colour}]  ({kind})")
    now = model_view(info.definition)
    if before is not None and info.review is Review.CHANGED:
        then = model_view(before.definition)
        changes = [
            line
            for line in difflib.unified_diff(then, now, lineterm="", n=2)
            if not line.startswith(("---", "+++", "@@"))
        ]
        if not changes:
            say("  The text the model reads is the same. Something it does not show changed.")
        for line in changes:
            style = {"+": "green", "-": "red"}.get(line[:1])
            text = escape(line)
            say(f"  [{style}]{text}[/{style}]" if style else f"  [dim]{text}[/dim]")
    else:
        for line in now:
            say(f"  {escape(line)}")
    if hides_text(
        " ".join(str(info.definition.get(key) or "") for key in ("name", "title", "description"))
        + str(info.definition.get("inputSchema"))
    ):
        say(
            "  [yellow]This definition contained hidden characters (invisible text or "
            "text-direction codes). They were removed from what is shown here and from what "
            "the model reads.[/yellow]"
        )


@app.command("trust")
def trust(name: NameArg, yes: Yes = False) -> None:
    """Read what a server's tools say about themselves, and accept them."""
    with opened() as agent:
        report = _look(agent, name)
        reviewable = [t for t in report.tools if not t.filtered]
        if not reviewable:
            fail(f"{name} offers no tools to review (after your allow_tools/deny_tools).")
        pending = [t for t in reviewable if t.review is not Review.TRUSTED]
        if not pending:
            say(
                f"[green]Nothing new:[/green] all {len(reviewable)} tool(s) of {escape(name)} "
                "are as you reviewed them."
            )
            return
        before = agent.mcp.pins.for_server(name)
        say(
            f"[bold]{escape(name)}[/bold] offers {len(reviewable)} tool(s); "
            f"{len(pending)} need your review."
        )
        for info in pending:
            _show_definition(info, before.get(info.name))
        gone = sorted(set(before) - {t.name for t in reviewable})
        if gone:
            say(
                f"\n{len(gone)} tool(s) you reviewed before are no longer offered: "
                + escape(", ".join(gone))
            )
        say(
            "\nThe model reads these descriptions as if they were instructions. Accept them "
            "only if you trust whoever runs the server."
        )
        if not yes and not typer.confirm("Trust these definitions?", default=False):
            say("Nothing was changed.")
            raise typer.Exit(1)
        count = agent.mcp.pin(name, report)
        say(
            f"[green]Reviewed {count} tool(s).[/green] A running agent picks this up on its "
            "next run; any change to a definition later will ask you again."
        )


# ---- add / remove -------------------------------------------------------------------------
@app.command("add")
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
    entry: dict[str, Any] = {"name": name}
    if url:
        if command:
            fail("Give either --url or a command, not both.", code=USAGE)
        entry.update(transport="sse" if sse else "http", url=url)
    elif command:
        entry.update(command=command[0], args=command[1:])
    else:
        fail("Give a command after `--`, or an address with --url.", code=USAGE)

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
            fail(f"A header needs a value: {item} (Name=Value).", code=USAGE)
        headers[key.strip()] = value
    if headers:
        entry["headers"] = headers
    if read_only:
        entry["read_only"] = list(read_only)
    if auto:
        entry["pin"] = "auto"

    with opened() as agent:
        try:
            add_server(agent.paths.config_file, entry)
        except ConfigEditError as exc:
            fail("Not added:", str(exc))
        say(f"[green]Added[/green] {escape(name)} to {escape(str(agent.paths.config_file))}")
        referenced = sorted(
            {ref for text in (*values.values(), *headers.values()) for ref in env_references(text)}
        )
        if referenced:
            say(
                f"It reads {', '.join(referenced)} from {escape(str(agent.paths.env_file))}; "
                "put the value(s) there."
            )
        say(
            f"Next: `pa mcp tools {escape(name)}` shows what it offers, "
            f"`pa mcp trust {escape(name)}` reviews it and switches its tools on."
        )


@app.command("remove")
def remove(name: NameArg, yes: Yes = False) -> None:
    """Take a server out of config.toml and forget what you reviewed of it."""
    with opened() as agent:
        if name not in agent.mcp.names():
            fail(f"No MCP server called {name}.")
        if not yes and not typer.confirm(f"Remove the MCP server {name}?", default=False):
            say("Kept.")
            raise typer.Exit(1)
        try:
            removed = remove_server(agent.paths.config_file, name)
        except ConfigEditError as exc:
            fail("Not removed:", str(exc))
        if not removed:
            fail(f"{name} is not in {agent.paths.config_file}.")
        agent.mcp.pins.forget(name)
        say(f"Removed {escape(name)}. A running agent stops using it when restarted.")
