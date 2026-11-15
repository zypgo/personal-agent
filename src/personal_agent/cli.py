"""`pa` command line."""

from __future__ import annotations

import asyncio
import atexit
import os
from collections.abc import Callable
from datetime import datetime

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from personal_agent import __version__
from personal_agent.app import App, build_app
from personal_agent.channels.cli import CliChannel
from personal_agent.clock import format_local
from personal_agent.config import DEFAULT_CONFIG_TOML, DEFAULT_ENV, ConfigError, load_env
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.worker import Worker
from personal_agent.doctor import live_checks, static_checks
from personal_agent.logs import setup_logging
from personal_agent.memory.store import INSTRUCTIONS_PATH, PROFILE_PATH, PROPOSALS_PATH, Actor
from personal_agent.paths import Paths

app = typer.Typer(
    name="pa",
    help="A local, always-on personal agent (OpenAI models, Anthropic engineering practices).",
    no_args_is_help=True,
    add_completion=False,
)
sessions_app = typer.Typer(help="List and clear conversations.", invoke_without_command=True)
outbox_app = typer.Typer(
    help="Messages waiting to be delivered, or whose delivery is unclear.",
    invoke_without_command=True,
)
memory_app = typer.Typer(
    help="Inspect the agent's memory and its change history.", no_args_is_help=True
)
prefs_app = typer.Typer(
    help="Your standing instructions (the agent can read but not change them).",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")
app.add_typer(outbox_app, name="outbox")
app.add_typer(memory_app, name="memory")
app.add_typer(prefs_app, name="prefs")

console = Console()

# Tests replace this to run the commands against a scripted model.
_build: Callable[[], App] = build_app

DEFAULT_SESSION = "main"


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"personal-agent {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    """personal-agent."""


def _open(*, recover: bool = False) -> App:
    try:
        opened = _build()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    setup_logging(opened.paths)
    if recover:
        opened.recover()
    return opened


def _ago(ts: float | None, now: float) -> str:
    if ts is None:
        return "-"
    seconds = max(0, int(now - ts))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return f"{seconds}s ago"


# ---- setup -------------------------------------------------------------------------------
@app.command()
def init() -> None:
    """Create the agent's home (~/.personal-agent), config and database. Safe to run again."""
    paths = Paths.resolve()
    paths.ensure()
    created: list[str] = []
    if not paths.config_file.exists():
        paths.config_file.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
        created.append(str(paths.config_file))
    if not paths.env_file.exists():
        paths.env_file.write_text(DEFAULT_ENV, encoding="utf-8")
        paths.env_file.chmod(0o600)
        created.append(str(paths.env_file))
    opened = _open()  # creates the database and the memory templates, and loads .env
    opened.close()
    console.print(f"[green]Ready.[/green] Home: {paths.home}")
    for item in created:
        console.print(f"  created {item}")
    if not os.environ.get("OPENAI_API_KEY"):
        console.print(
            f"\nNext: put your key in [bold]{paths.env_file}[/bold] (OPENAI_API_KEY=...), then run [bold]pa doctor[/bold]."
        )
    else:
        console.print("\nNext: run [bold]pa doctor[/bold], then [bold]pa chat[/bold].")


@app.command()
def doctor(
    live: bool = typer.Option(
        False, "--live", help="Also make two real model calls (a few cents)."
    ),
) -> None:
    """Check the setup and explain how to fix what is wrong."""
    paths = Paths.resolve()
    load_env(paths)
    checks = static_checks(paths)
    skipped_live = live and any(c.status == "fail" for c in checks)
    if live and not skipped_live:
        opened = _open(recover=True)
        try:
            with console.status("[dim]calling the model...[/dim]"):
                checks += asyncio.run(live_checks(opened))
        finally:
            opened.close()
    marks = {
        "ok": "[green]ok[/green]  ",
        "warn": "[yellow]warn[/yellow]",
        "fail": "[red]FAIL[/red]",
    }
    for check in checks:
        console.print(f"{marks[check.status]} {check.name}: {escape(check.detail)}")
        if check.fix and check.status != "ok":
            console.print(f"       [dim]-> {escape(check.fix)}[/dim]")
    if skipped_live:
        console.print("[dim]Skipped the live check: fix the failures above first.[/dim]")
    if any(c.status == "fail" for c in checks):
        raise typer.Exit(1)


# ---- talking to the agent ----------------------------------------------------------------
async def _turn(opened: App, session_key: str) -> bool:
    """Run everything ready in a session, then deliver what the agent said. True if no failure."""
    worker = Worker(opened)
    ok = True
    with console.status("[dim]thinking...[/dim]"):
        while (
            result := await worker.run_once(session_key=session_key, on_failure="drop")
        ) is not None:
            if result.failure is not None:
                ok = False
                console.print(
                    f"[red]Error:[/red] {result.failure.user_hint or result.failure.summary}"
                )
    await Dispatcher(opened, {"cli": CliChannel(console)}).deliver_once()
    return ok


def _submit(opened: App, text: str, session_key: str) -> None:
    opened.inbox.add(
        NewEvent(source="cli", kind=Kind.MESSAGE, session_key=session_key, payload={"text": text})
    )


@app.command()
def ask(
    text: str = typer.Argument(..., help="What to ask."),
    session: str = typer.Option(
        DEFAULT_SESSION, "--session", "-s", help="Conversation to continue."
    ),
) -> None:
    """Ask once and print the answer."""
    opened = _open(recover=True)
    try:
        _submit(opened, text, session)
        if not asyncio.run(_turn(opened, session)):
            raise typer.Exit(1)
    finally:
        opened.close()


HELP_TEXT = """\
/new       start a fresh conversation (the old one stays in the log)
/status    what is queued, what ran, what it cost
/memory    show the agent's memory directory
/runs      recent runs
/help      this list
/exit      leave (Ctrl-D works too)"""


def _enable_history(opened: App) -> None:
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows
        return
    history = opened.paths.home / ".chat_history"
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(1000)
    atexit.register(lambda: _save_history(readline, history))


def _save_history(readline, history) -> None:
    try:
        readline.write_history_file(history)
        history.chmod(0o600)
    except OSError:
        pass


def chat_loop(opened: App, session_key: str, read_line: Callable[[str], str] = input) -> None:
    """The interactive loop. One event loop for the whole conversation, so the HTTP connection
    pool is reused, and Ctrl-C cancels only the running turn."""
    console.print(
        Panel(
            f"[bold]{opened.config.agent.name}[/bold] · model {opened.config.model.main} · session '{session_key}'\n"
            "[dim]/help for commands, Ctrl-D to leave[/dim]",
            expand=False,
        )
    )
    with asyncio.Runner() as runner:
        # Anything the agent wanted to say while you were away.
        runner.run(Dispatcher(opened, {"cli": CliChannel(console)}).deliver_once())
        while True:
            try:
                line = read_line("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                break
            if not line:
                continue
            if line.startswith("/"):
                if not _slash(opened, line, session_key):
                    break
                continue
            _submit(opened, line, session_key)
            try:
                runner.run(_turn(opened, session_key))
            except KeyboardInterrupt:
                dropped = opened.inbox.drop_pending(session_key, "interrupted by the owner")
                console.print(f"[dim]interrupted ({dropped} message(s) dropped)[/dim]")


def _slash(opened: App, line: str, session_key: str) -> bool:
    """Handle a /command. Returns False to leave the chat."""
    command = line.split()[0].lower()
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        console.print(HELP_TEXT)
    elif command == "/new":
        opened.sessions.clear(session_key)
        console.print("[dim]Started a fresh conversation.[/dim]")
    elif command == "/status":
        _print_status(opened)
    elif command == "/memory":
        console.print(opened.memory.view("/memories"), markup=False)
    elif command == "/runs":
        _print_runs(opened, 8)
    else:
        console.print(f"[yellow]Unknown command {command}.[/yellow] Try /help.")
    return True


@app.command()
def chat(
    session: str = typer.Option(
        DEFAULT_SESSION, "--session", "-s", help="Conversation to continue."
    ),
) -> None:
    """Talk to the agent."""
    opened = _open(recover=True)
    _enable_history(opened)
    try:
        chat_loop(opened, session)
    finally:
        opened.close()


# ---- inspecting state --------------------------------------------------------------------
def _print_status(opened: App) -> None:
    now = opened.clock.now()
    cfg = opened.config
    midnight = (
        datetime.fromtimestamp(now, opened.tz)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    console.print(
        f"[bold]{cfg.agent.name}[/bold] · {cfg.model.main} · {format_local(now, opened.tz)}"
    )
    inbox, outbox = opened.inbox.counts(), opened.outbox.counts()
    console.print(f"inbox   {_counts(inbox)}")
    console.print(f"outbox  {_counts(outbox)}")
    console.print(f"runs    {_counts(opened.runs.summary())}")
    console.print(
        f"spend   today ${opened.runs.cost_since(midnight):.4f} · last 7 days ${opened.runs.cost_since(now - 7 * 86400):.4f}"
    )
    if outbox["maybe_sent"] or outbox["failed"]:
        console.print("[yellow]Some messages may not have reached you: run `pa outbox`.[/yellow]")


def _counts(counts: dict[str, int]) -> str:
    shown = [f"{name} {n}" for name, n in counts.items() if n]
    return ", ".join(shown) if shown else "[dim]empty[/dim]"


@app.command()
def status() -> None:
    """Queues, runs and spend."""
    opened = _open()
    try:
        _print_status(opened)
    finally:
        opened.close()


def _print_runs(opened: App, limit: int) -> None:
    now = opened.clock.now()
    table = Table(box=None, pad_edge=False)
    for column in ("id", "when", "session", "profile", "status", "calls", "tools", "cost"):
        table.add_column(column)
    for run in opened.runs.recent(limit):
        table.add_row(
            run.id,
            _ago(run.started_at, now),
            run.session_key,
            run.profile + (" (tainted)" if run.tainted else ""),
            run.status,
            str(run.requests),
            str(run.tool_calls),
            f"${run.cost_usd:.4f}",
        )
    console.print(table)


@app.command()
def runs(limit: int = typer.Option(15, "--limit", "-n")) -> None:
    """Recent runs."""
    opened = _open()
    try:
        _print_runs(opened, limit)
    finally:
        opened.close()


@app.command()
def run(run_id: str = typer.Argument(..., help="A run id from `pa runs`.")) -> None:
    """Everything recorded about one run."""
    opened = _open()
    try:
        record = opened.runs.get(run_id)
        if record is None:
            console.print(f"[red]No run {run_id}.[/red]")
            raise typer.Exit(1)
        console.print(
            f"[bold]{record.id}[/bold] {record.status} · {record.profile} · session {record.session_key}"
        )
        console.print(
            f"{record.requests} model calls, {record.tool_calls} tool calls, "
            f"{record.input_tokens} in ({record.cached_tokens} cached) / {record.output_tokens} out, ${record.cost_usd:.4f}"
        )
        if record.error:
            console.print(f"[red]error:[/red] {record.error}")
        for row in opened.sessions.transcript(record.session_key, limit=5000):
            if row["run_id"] != run_id or row["kind"] != "item":
                continue
            item = row["item"]
            kind = item.get("role") or item.get("type")
            body = (
                item.get("content")
                or item.get("arguments")
                or item.get("output")
                or item.get("name")
                or ""
            )
            if isinstance(body, list):
                body = " ".join(
                    str(part.get("text", "")) for part in body if isinstance(part, dict)
                )
            console.print(f"[dim]{kind}[/dim] {str(body)[:600]}")
    finally:
        opened.close()


@sessions_app.callback()
def _sessions_list(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    opened = _open()
    try:
        now = opened.clock.now()
        for info in opened.sessions.list_sessions():
            console.print(
                f"{info.session_key:<30} {info.items:>5} items  {_ago(info.last_activity, now)}"
            )
    finally:
        opened.close()


@sessions_app.command("clear")
def sessions_clear(key: str = typer.Argument(DEFAULT_SESSION)) -> None:
    """Hide a conversation from the model (the log keeps it)."""
    opened = _open()
    try:
        opened.sessions.clear(key)
        console.print(f"Cleared session '{key}'.")
    finally:
        opened.close()


@outbox_app.callback()
def _outbox_list(ctx: typer.Context, status: str | None = typer.Option(None, "--status")) -> None:
    if ctx.invoked_subcommand is not None:
        return
    opened = _open()
    try:
        now = opened.clock.now()
        for message in opened.outbox.list(status=status, limit=30):
            preview = message.text.replace("\n", " ")[:70]
            console.print(
                f"#{message.id:<5} {message.status:<10} {message.channel:<9} {message.urgency:<7} "
                f"{_ago(message.created_at, now):<9} {preview}"
            )
    finally:
        opened.close()


@outbox_app.command("dismiss")
def outbox_dismiss(
    message_id: int | None = typer.Argument(None),
    all_: bool = typer.Option(False, "--all", help="Dismiss every unresolved message."),
) -> None:
    """Stop the agent worrying about messages that failed or have an unknown outcome."""
    opened = _open()
    try:
        ids = (
            [m.id for m in opened.outbox.unresolved()]
            if all_
            else ([message_id] if message_id else [])
        )
        dismissed = sum(opened.outbox.cancel(i) for i in ids)
        console.print(f"Dismissed {dismissed} message(s).")
    finally:
        opened.close()


# ---- memory and preferences --------------------------------------------------------------
@memory_app.command("view")
def memory_view(path: str = typer.Argument("/memories")) -> None:
    """Show a memory directory or file."""
    opened = _open()
    try:
        console.print(opened.memory.view(path), markup=False)
    finally:
        opened.close()


@memory_app.command("history")
def memory_history(
    path: str = typer.Argument(...), limit: int = typer.Option(10, "--limit", "-n")
) -> None:
    """Who changed a file, when, and how."""
    opened = _open()
    try:
        for row in opened.memory.history(path, limit):
            console.print(
                f"version {row['id']:<5} {row['action']:<8} by {row['actor']:<6} "
                f"{format_local(row['created_at'], opened.tz)}"
            )
    finally:
        opened.close()


@memory_app.command("restore")
def memory_restore(version_id: int = typer.Argument(...)) -> None:
    """Undo one change: put the file back as it was before that version."""
    opened = _open()
    try:
        console.print(opened.memory.restore(version_id), markup=False)
    finally:
        opened.close()


@prefs_app.command("show")
def prefs_show() -> None:
    """Print your standing instructions and profile."""
    opened = _open()
    try:
        for title, path in (("instructions", INSTRUCTIONS_PATH), ("profile", PROFILE_PATH)):
            console.print(f"[bold]{title}[/bold] ({path})")
            text = opened.memory.read_text(path)
            console.print(escape(text) if text else "[dim](empty)[/dim]")
    finally:
        opened.close()


@prefs_app.command("edit")
def prefs_edit(
    profile: bool = typer.Option(
        False, "--profile", help="Edit profile.md instead of instructions.md."
    ),
) -> None:
    """Edit your standing instructions in $EDITOR. Takes effect on the agent's next run."""
    opened = _open()
    try:
        path = PROFILE_PATH if profile else INSTRUCTIONS_PATH
        edited = click.edit(opened.memory.read_text(path) or "", extension=".md")
        if edited is None:
            console.print("No changes.")
            return
        opened.memory.write_text(path, edited, actor=Actor.USER)
        console.print(f"Saved {path}.")
    finally:
        opened.close()


@prefs_app.command("proposals")
def prefs_proposals() -> None:
    """Changes the agent suggests for your preferences (it cannot make them itself)."""
    opened = _open()
    try:
        text = opened.memory.read_text(PROPOSALS_PATH)
        console.print(escape(text) if text else "[dim](none)[/dim]")
    finally:
        opened.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
