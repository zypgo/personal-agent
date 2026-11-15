"""`pa` command line."""

from __future__ import annotations

import asyncio
import atexit
import os
import re
import signal
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated

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
from personal_agent.commands import mcp as mcp_commands
from personal_agent.commands import plugins as plugins_commands
from personal_agent.commands import skills as skills_commands
from personal_agent.config import DEFAULT_CONFIG_TOML, DEFAULT_ENV, ConfigError, load_env
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import Kind, NewEvent
from personal_agent.core.timers import TimerError
from personal_agent.daemon import AlreadyRunning, Daemon, Engine, daemon_state
from personal_agent.doctor import capability_checks, live_checks, static_checks
from personal_agent.logs import setup_logging
from personal_agent.memory.store import INSTRUCTIONS_PATH, PROFILE_PATH, PROPOSALS_PATH, Actor
from personal_agent.paths import Paths
from personal_agent.skills.install import install_bundled

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
timers_app = typer.Typer(
    help="Reminders and recurring jobs the agent (or you) set up.", invoke_without_command=True
)
budget_app = typer.Typer(help="Spending limits and today's spend.", invoke_without_command=True)
memory_app = typer.Typer(
    help="Inspect the agent's memory and its change history.", no_args_is_help=True
)
prefs_app = typer.Typer(
    help="Your standing instructions (the agent can read but not change them).",
    no_args_is_help=True,
)
app.add_typer(sessions_app, name="sessions")
app.add_typer(outbox_app, name="outbox")
app.add_typer(timers_app, name="timers")
app.add_typer(budget_app, name="budget")
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


def _open() -> App:
    try:
        opened = _build()
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc
    setup_logging(opened.paths)
    return opened


app.add_typer(skills_commands.build(lambda: _open(), console), name="skills")
app.add_typer(mcp_commands.build(lambda: _open(), console), name="mcp")
app.add_typer(plugins_commands.build(lambda: _open(), console), name="plugins")


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
    starter = install_bundled(paths)
    console.print(f"[green]Ready.[/green] Home: {paths.home}")
    for item in created:
        console.print(f"  created {item}")
    if starter:
        console.print(f"  added starter skills: {', '.join(starter)} (see `pa skills`)")
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
    checks += capability_checks(paths)  # says nothing if the config is broken (already reported)
    skipped_live = live and any(c.status == "fail" for c in checks)
    if live and not skipped_live:
        opened = _open()
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
async def _turn(engine: Engine, session_key: str, event_id: int | None = None) -> bool:
    """Get everything ready in a session processed, then show what the agent said.

    Returns False if the message could not be handled (the reason has been printed). With a
    background service running, "processed" means the service did it and we waited."""
    opened = engine.app
    dispatcher = Dispatcher(opened, {"cli": CliChannel(console)})

    async def show() -> None:
        await dispatcher.deliver_once()

    with console.status("[dim]thinking...[/dim]"):
        results = await engine.settle(session_key, tick=show)
    ok = True
    for result in results:
        if result.failure is not None:
            ok = False
            console.print(f"[red]Error:[/red] {result.failure.user_hint or result.failure.summary}")
    await dispatcher.deliver_once()
    if event_id is not None and not engine.inline:
        ok = _service_handled(opened, event_id)
    return ok


def _service_handled(opened: App, event_id: int) -> bool:
    event = opened.inbox.get(event_id)
    if event is None or event["status"] == "done":
        return True
    if event["status"] == "dead":
        console.print(f"[red]Error:[/red] {escape(str(event['last_error'] or 'it failed'))}")
    else:
        console.print(
            "[yellow]The agent could not finish this yet; the background service will retry.[/yellow]"
        )
    return False


def _submit(opened: App, text: str, session_key: str) -> int:
    return opened.inbox.add(
        NewEvent(source="cli", kind=Kind.MESSAGE, session_key=session_key, payload={"text": text})
    ).id


@app.command()
def ask(
    text: str = typer.Argument(..., help="What to ask."),
    session: str = typer.Option(
        DEFAULT_SESSION, "--session", "-s", help="Conversation to continue."
    ),
) -> None:
    """Ask once and print the answer."""
    opened = _open()
    engine = Engine.acquire(opened)
    try:
        event_id = _submit(opened, text, session)
        if not asyncio.run(_turn(engine, session, event_id)):
            raise typer.Exit(1)
    finally:
        engine.close()
        opened.close()


HELP_TEXT = """\
/new              start a fresh conversation (the old one stays in the log)
/status           what is queued, what ran, what it cost
/approvals        actions waiting for your yes or no
/approve [N|all]  allow a waiting action
/deny [N|all]     refuse it; add a few words after the number to tell the agent why
/memory           show the agent's memory directory
/runs             recent runs
/help             this list
/exit             leave (Ctrl-D works too)"""


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
    engine = Engine.acquire(opened)
    engine.keep_services = True  # MCP servers stay up between messages; _chat closes them
    try:
        _chat(engine, session_key, read_line)
    finally:
        engine.close()


def _chat(engine: Engine, session_key: str, read_line: Callable[[str], str]) -> None:
    opened = engine.app
    where = "" if engine.inline else " · background service"
    console.print(
        Panel(
            f"[bold]{opened.config.agent.name}[/bold] · model {opened.config.model.main} · session '{session_key}'{where}\n"
            "[dim]/help for commands, Ctrl-D to leave[/dim]",
            expand=False,
        )
    )
    with asyncio.Runner() as runner:
        try:
            # Anything the agent wanted to say while you were away.
            runner.run(Dispatcher(opened, {"cli": CliChannel(console)}).deliver_once())
            _remind_of_approvals(opened)
            while True:
                try:
                    line = read_line("you> ").strip()
                except (EOFError, KeyboardInterrupt):
                    console.print()
                    break
                if not line:
                    continue
                try:
                    if line.startswith("/"):
                        if not _slash(engine, line, session_key, runner.run):
                            break
                        continue
                    event_id = _submit(opened, line, session_key)
                    runner.run(_turn(engine, session_key, event_id))
                except KeyboardInterrupt:
                    _interrupted(engine, session_key)
        finally:
            runner.run(opened.stop_services())


def _interrupted(engine: Engine, session_key: str) -> None:
    if engine.inline:
        dropped = engine.app.inbox.drop_pending(session_key, "interrupted by the owner")
        console.print(f"[dim]interrupted ({dropped} message(s) dropped)[/dim]")
    else:
        console.print(
            "[dim]Stopped waiting. The background service is still working on it; "
            "the answer will be shown when you next open the chat.[/dim]"
        )


def _slash(
    engine: Engine, line: str, session_key: str, run: Callable[[Awaitable[bool]], bool]
) -> bool:
    """Handle a /command. Returns False to leave the chat."""
    opened = engine.app
    words = line.split()
    command = words[0].lower()
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
    elif command == "/approvals":
        _print_approvals(opened, include_decided=False)
    elif command in ("/approve", "/deny"):
        targets, note = _split_targets(words[1:])
        _answer_approvals(engine, command == "/approve", targets, note, run)
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
    opened = _open()
    _enable_history(opened)
    try:
        chat_loop(opened, session)
    finally:
        opened.close()


# ---- approvals ---------------------------------------------------------------------------
_APPROVAL_ID = re.compile(r"#?(\d+)$")


def _split_targets(words: list[str]) -> tuple[list[str], str | None]:
    """ "3 4 not now" -> (["3", "4"], "not now"): leading numbers (or "all") pick the requests,
    whatever follows is the note for the agent."""
    targets: list[str] = []
    for index, word in enumerate(words):
        if word.lower() == "all" or _APPROVAL_ID.match(word):
            targets.append(word.lower())
        else:
            return targets, " ".join(words[index:])
    return targets, None


def _pick_approvals(opened: App, targets: list[str]) -> list[int] | None:
    pending = opened.approvals.pending()
    if not pending:
        console.print("[dim]Nothing is waiting for your answer.[/dim]")
        return None
    if not targets:
        if len(pending) == 1:
            return [pending[0].id]
        ids = ", ".join(f"#{a.id}" for a in pending)
        console.print(
            f"[yellow]Several actions are waiting ({ids}): name one, or say `all`.[/yellow]"
        )
        return None
    if "all" in targets:
        return [a.id for a in pending]
    return [int(m.group(1)) for t in targets if (m := _APPROVAL_ID.match(t))]


def _answer_approvals(
    engine: Engine,
    approve: bool,
    targets: list[str],
    note: str | None,
    run: Callable[[Awaitable[bool]], bool],
) -> None:
    """Record the owner's answer, then let the paused runs continue (here, or in the service)."""
    opened = engine.app
    ids = _pick_approvals(opened, targets)
    if ids is None:
        return
    resume: list[str] = []
    for approval_id in ids:
        result = opened.approvals.decide(approval_id, approve, by="owner:cli", note=note)
        colour = "green" if result.changed else "yellow"
        console.print(f"[{colour}]{escape(result.message)}[/{colour}]")
        if result.resume_queued and result.approval is not None:
            resume.append(result.approval.session_key)
    for session_key in dict.fromkeys(resume):
        run(_turn(engine, session_key))


def _remind_of_approvals(opened: App) -> None:
    waiting = opened.approvals.pending()
    if waiting:
        console.print(
            f"[yellow]{len(waiting)} action(s) are waiting for your answer:[/yellow] "
            "/approvals to see them, /approve or /deny to answer."
        )


def _print_approvals(opened: App, *, include_decided: bool) -> None:
    now = opened.clock.now()
    rows = opened.approvals.recent(30) if include_decided else opened.approvals.pending()
    if not rows:
        console.print("[dim]Nothing is waiting for your answer.[/dim]")
        return
    table = Table(box=None, pad_edge=False)
    for column in ("id", "status", "tool", "what", "session", "asked", "expires"):
        table.add_column(column)
    for item in rows:
        table.add_row(
            f"#{item.id}",
            item.status,
            item.tool,
            escape(item.summary[:80]),
            item.session_key,
            _ago(item.created_at, now),
            f"in {_span(item.expires_at - now)}" if item.status == "pending" else "-",
        )
    console.print(table)


def _span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"


@app.command()
def approvals(
    all_: bool = typer.Option(False, "--all", "-a", help="Also show answered requests."),
) -> None:
    """Actions the agent is waiting for your yes or no on."""
    opened = _open()
    try:
        _print_approvals(opened, include_decided=all_)
    finally:
        opened.close()


def _decide_command(approve: bool, words: list[str], note: str | None) -> None:
    targets, trailing = _split_targets(words)
    opened = _open()
    engine = Engine.acquire(opened)
    try:
        _answer_approvals(engine, approve, targets, note or trailing, asyncio.run)
    finally:
        engine.close()
        opened.close()


@app.command()
def approve(
    ids: Annotated[
        list[str] | None, typer.Argument(help="Request numbers from `pa approvals`, or `all`.")
    ] = None,
    note: str | None = typer.Option(None, "--note", "-m", help="A word for the agent."),
) -> None:
    """Allow a waiting action; the paused work continues."""
    _decide_command(True, ids or [], note)


@app.command()
def deny(
    ids: Annotated[
        list[str] | None, typer.Argument(help="Request numbers from `pa approvals`, or `all`.")
    ] = None,
    note: str | None = typer.Option(None, "--note", "-m", help="Why, so the agent can adapt."),
) -> None:
    """Refuse a waiting action; the agent is told and carries on without it."""
    _decide_command(False, ids or [], note)


# ---- running in the background -----------------------------------------------------------
@app.command()
def serve(
    show_messages: bool = typer.Option(
        False,
        "--console",
        help="Print the agent's terminal messages here (otherwise they wait for `pa chat`).",
    ),
) -> None:
    """Run the agent in the foreground: timers, background events and delivery, until Ctrl-C."""
    opened = _open()
    setup_logging(opened.paths, console=True)
    channels = {"cli": CliChannel(console)} if show_messages else {}
    daemon = Daemon(opened, channels, grace_s=opened.config.runtime.shutdown_grace_s)
    try:
        asyncio.run(_serve(daemon))
    except AlreadyRunning as exc:
        console.print(f"[red]{exc}.[/red] Stop it first (or use `pa chat` to talk to it).")
        raise typer.Exit(1) from exc
    finally:
        opened.close()


async def _serve(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        if daemon.stopping:  # a second Ctrl-C means "now"
            raise SystemExit(130)
        console.print("[dim]Stopping: letting running work finish (Ctrl-C again to force)...[/dim]")
        daemon.stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, on_signal)
    running = asyncio.create_task(daemon.run())
    ready = asyncio.create_task(daemon.wait_ready())
    await asyncio.wait({running, ready}, return_when=asyncio.FIRST_COMPLETED)
    if running.done():  # it could not start (for example another copy holds the lock)
        ready.cancel()
        await running
        return
    cfg = daemon.app.config
    console.print(
        f"[bold]{cfg.agent.name}[/bold] is running · model {cfg.model.main} · Ctrl-C to stop. "
        "Talk to it from another terminal with `pa chat`."
    )
    await running
    console.print("[dim]Stopped.[/dim]")


# ---- inspecting state --------------------------------------------------------------------
def _print_status(opened: App) -> None:
    now = opened.clock.now()
    cfg = opened.config
    console.print(
        f"[bold]{cfg.agent.name}[/bold] · {cfg.model.main} · {format_local(now, opened.tz)}"
    )
    state = daemon_state(opened)
    if state.kind == "daemon":
        console.print(
            f"agent   [green]running in the background[/green] (pid {state.pid}, "
            f"heartbeat {_span(state.heartbeat_age_s or 0)} ago)"
        )
    elif state.kind == "terminal":
        console.print(f"agent   run by a terminal session (pid {state.pid})")
    else:
        console.print(
            "agent   [yellow]not running[/yellow]: timers and background events wait until "
            "`pa serve` (or `pa chat`) runs"
        )
    inbox, outbox = opened.inbox.counts(), opened.outbox.counts()
    console.print(f"inbox   {_counts(inbox)}")
    console.print(f"outbox  {_counts(outbox)}")
    console.print(f"runs    {_counts(opened.runs.summary())}")
    timers = opened.timers.list()
    if timers:
        upcoming = opened.timers.next_due()
        soon = f", next in {_span(upcoming - now)}" if upcoming is not None else ""
        console.print(f"timers  {len(timers)} active{soon}")
    waiting = opened.approvals.pending()
    if waiting:
        console.print(
            f"[yellow]approvals {len(waiting)} waiting for your answer[/yellow] (`pa approvals`)"
        )
    budget = opened.budget.status()
    console.print(
        f"spend   today ${budget.spent_today:.4f} of ${budget.daily_limit:.2f} · "
        f"last 7 days ${opened.runs.cost_since(now - 7 * 86400):.4f}"
    )
    if budget.remaining_today <= 0:
        console.print(
            f"[yellow]Paused for the budget until local midnight (in {_span(budget.resets_in_s)}). "
            "`pa budget extend 2` lets it continue today.[/yellow]"
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


# ---- timers ------------------------------------------------------------------------------
_DURATION = re.compile(r"(\d+)\s*([smhd])$", re.IGNORECASE)
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(text: str) -> float:
    match = _DURATION.match(text.strip())
    if not match:
        raise typer.BadParameter("use a number and a unit, such as 90s, 45m, 2h or 1d")
    return int(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]


def _parse_local_time(opened: App, text: str) -> float:
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise typer.BadParameter('use a local time like "2026-11-20 09:30"') from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=opened.tz)
    return moment.timestamp()


def _print_timers(opened: App, *, include_finished: bool) -> None:
    now = opened.clock.now()
    rows = opened.timers.list(include_finished=include_finished)
    if not rows:
        console.print(
            "[dim]No timers. Ask the agent to remind you of something, or use `pa timers add`.[/dim]"
        )
        return
    table = Table(box=None, pad_edge=False)
    for column in ("id", "name", "status", "repeats", "next", "in", "fired", "by"):
        table.add_column(column)
    for timer in rows:
        live = timer.status in ("active", "paused")
        table.add_row(
            str(timer.id),
            escape(timer.name),
            timer.status,
            f"{timer.cron} ({timer.tz})" if timer.cron else "once",
            format_local(timer.due_at, opened.tz) if live else "-",
            _span(timer.due_at - now) if timer.status == "active" else "-",
            str(timer.fire_count),
            timer.created_by,
        )
    console.print(table)


@timers_app.callback()
def _timers_list(
    ctx: typer.Context,
    all_: bool = typer.Option(False, "--all", "-a", help="Include finished and cancelled timers."),
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    opened = _open()
    try:
        _print_timers(opened, include_finished=all_)
    finally:
        opened.close()


@timers_app.command("add")
def timers_add(
    name: str = typer.Argument(..., help="A short unique name, such as morning-brief."),
    prompt: str = typer.Argument(..., help="What the agent should do when it fires."),
    cron: str | None = typer.Option(None, "--cron", help='Recurring, e.g. "0 8 * * 1-5".'),
    at: str | None = typer.Option(None, "--at", help='Once, at a local time: "2026-11-20 09:30".'),
    in_: str | None = typer.Option(None, "--in", help="Once, after a delay: 90s, 45m, 2h, 1d."),
    tz: str | None = typer.Option(None, "--tz", help="Time zone for --cron (default: yours)."),
) -> None:
    """Schedule something for the agent to do later (it tells you what it finds)."""
    if sum(option is not None for option in (cron, at, in_)) != 1:
        raise typer.BadParameter("give exactly one of --cron, --at or --in")
    opened = _open()
    try:
        try:
            if cron is not None:
                timer = opened.timers.add_schedule(
                    name=name, prompt=prompt, cron=cron, tz=tz, created_by="owner"
                )
            else:
                due = (
                    _parse_local_time(opened, at)
                    if at is not None
                    else opened.clock.now() + _parse_duration(in_ or "")
                )
                timer = opened.timers.add_followup(
                    name=name, prompt=prompt, due_at=due, created_by="owner"
                )
        except TimerError as exc:
            console.print(f"[red]{escape(str(exc))}[/red]")
            raise typer.Exit(1) from exc
        console.print(f"Scheduled '{timer.name}' for {format_local(timer.due_at, opened.tz)}.")
        if daemon_state(opened).kind == "stopped":
            console.print(
                "[dim]It fires while the agent is running: start it with `pa serve`.[/dim]"
            )
    finally:
        opened.close()


_TIMER_DONE = {"cancel": "Cancelled", "pause": "Paused", "resume": "Resumed"}


def _timer_action(verb: str, ref: str) -> None:
    opened = _open()
    try:
        if not getattr(opened.timers, verb)(ref):
            console.print(f"[yellow]Could not {verb} '{escape(ref)}': no such timer.[/yellow]")
            raise typer.Exit(1)
        console.print(f"{_TIMER_DONE[verb]} '{escape(ref)}'.")
    finally:
        opened.close()


@timers_app.command("cancel")
def timers_cancel(ref: str = typer.Argument(..., help="Timer name or id.")) -> None:
    """Stop a timer for good."""
    _timer_action("cancel", ref)


@timers_app.command("pause")
def timers_pause(ref: str = typer.Argument(..., help="Timer name or id.")) -> None:
    """Keep a timer but stop it firing until resumed."""
    _timer_action("pause", ref)


@timers_app.command("resume")
def timers_resume(ref: str = typer.Argument(..., help="Timer name or id.")) -> None:
    """Un-pause a timer (a recurring one continues from its next regular time)."""
    _timer_action("resume", ref)


# ---- audit and budget --------------------------------------------------------------------
@app.command()
def audit(
    limit: int = typer.Option(25, "--limit", "-n"),
    tool: str | None = typer.Option(None, "--tool", help="Only this tool."),
    verdict: str | None = typer.Option(None, "--verdict", help="allow, ask or deny."),
    run_id: str | None = typer.Option(None, "--run", help="Only this run (from `pa runs`)."),
) -> None:
    """What the agent actually did: every tool call, what the policy decided and how it ended."""
    opened = _open()
    try:
        now = opened.clock.now()
        rows = opened.audit.recent(limit, run_id=run_id, tool=tool, verdict=verdict)
        if not rows:
            console.print("[dim]Nothing recorded yet.[/dim]")
            return
        table = Table(box=None, pad_edge=False)
        for column in ("when", "tool", "decision", "how it ended", "what"):
            table.add_column(column)
        for row in rows:
            decision = row.verdict + (f" ({row.layer})" if row.layer else "")
            table.add_row(
                _ago(row.ts, now),
                row.tool + (" [yellow]tainted[/yellow]" if row.tainted else ""),
                decision,
                row.outcome or "-",
                escape((row.detail or row.args or "")[:70]),
            )
        console.print(table)
    finally:
        opened.close()


@budget_app.callback()
def _budget_show(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    opened = _open()
    try:
        budget = opened.budget.status()
        console.print(
            f"today      ${budget.spent_today:.4f} of ${budget.daily_limit:.2f}"
            + (f" (including ${budget.extra_today:.2f} you added)" if budget.extra_today else "")
        )
        console.print(f"per run    ${budget.per_run_limit:.2f}")
        console.print(f"resets     in {_span(budget.resets_in_s)} (local midnight)")
        if budget.remaining_today <= 0:
            console.print("[yellow]Paused: `pa budget extend 2` lets work continue today.[/yellow]")
    finally:
        opened.close()


@budget_app.command("extend")
def budget_extend(
    amount: float = typer.Argument(..., help="Dollars to add to today's limit."),
) -> None:
    """Raise today's limit and let paused work continue. Tomorrow the normal limit applies."""
    if amount <= 0:
        raise typer.BadParameter("the amount must be positive")
    opened = _open()
    try:
        limit, released = opened.extend_budget(amount)
        console.print(f"Today's limit is now ${limit:.2f}; {released} waiting event(s) released.")
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
