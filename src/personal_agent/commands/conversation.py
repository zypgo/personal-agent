"""Talking to the agent from the terminal: one question, or a chat, and answering what it asks.

This is the part of the command line that runs the agent, so it imports the runtime. The command
modules import it only inside the command that needs it (see `common.py`).

Who runs the agent is decided by `Engine`: this process, if nothing else does, or the background
service, in which case this process hands the message over and waits for the answer.
"""

from __future__ import annotations

import asyncio
import atexit
from collections.abc import Callable, Coroutine
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any

from rich.markup import escape
from rich.panel import Panel

from personal_agent.channels.cli import CliChannel
from personal_agent.commands import views
from personal_agent.commands.common import console, echo, say, warn
from personal_agent.core.approvals import choose_approvals, split_targets
from personal_agent.core.dispatcher import Dispatcher
from personal_agent.core.events import Kind, NewEvent
from personal_agent.daemon import Engine

if TYPE_CHECKING:
    from personal_agent.app import App

RunTurn = Callable[[Coroutine[Any, Any, bool]], bool]
"""Runs one turn to its end: `asyncio.run`, or the chat loop's own event loop."""

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


# ---- one turn ----------------------------------------------------------------------------
def submit(app: App, text: str, session_key: str) -> int:
    """Put the owner's message in the inbox (and the transcript), and say which event it is."""
    with app.db.tx():
        added = app.inbox.add(
            NewEvent(
                source="cli", kind=Kind.MESSAGE, session_key=session_key, payload={"text": text}
            )
        )
        app.transcript.add_in("cli", text)
    return added.id


async def turn(engine: Engine, session_key: str, event_id: int | None = None) -> bool:
    """Get everything ready in a session processed, then show what the agent said.

    Returns False if the message could not be handled (the reason has been printed). With a
    background service running, "processed" means the service did it and we waited."""
    app = engine.app
    dispatcher = Dispatcher(app, {"cli": CliChannel(console)})

    async def show() -> None:
        await dispatcher.deliver_once()

    with console.status("[dim]thinking...[/dim]"):
        results = await engine.settle(session_key, tick=show)
    ok = True
    for result in results:
        if result.failure is not None:
            ok = False
            say(f"[red]Error:[/red] {escape(result.failure.user_hint or result.failure.summary)}")
    await dispatcher.deliver_once()
    if event_id is not None and not engine.inline:
        ok = _service_handled(app, event_id)
    return ok


def _service_handled(app: App, event_id: int) -> bool:
    event = app.inbox.get(event_id)
    if event is None or event["status"] == "done":
        return True
    if event["status"] == "dead":
        say(f"[red]Error:[/red] {escape(str(event['last_error'] or 'it failed'))}")
    else:
        warn("The agent could not finish this yet; the background service will retry.")
    return False


def ask_once(app: App, text: str, session_key: str) -> bool:
    """`pa ask`: one message, one answer. False if it could not be answered."""
    with Engine.acquire(app) as engine:
        event_id = submit(app, text, session_key)
        return asyncio.run(turn(engine, session_key, event_id))


# ---- approvals ---------------------------------------------------------------------------
def answer_approvals(
    engine: Engine, approve: bool, targets: list[str], note: str | None, run: RunTurn
) -> None:
    """Record the owner's answer, then let the paused runs continue (here, or in the service)."""
    app = engine.app
    ids, problem = choose_approvals(app.approvals.pending(), targets)
    if problem:
        colour = "dim" if ids is None and "Nothing" in problem else "yellow"
        say(f"[{colour}]{escape(problem)}[/{colour}]")
    if ids is None:
        return
    resume: list[str] = []
    for approval_id in ids:
        result = app.approvals.decide(approval_id, approve, by="owner:cli", note=note)
        colour = "green" if result.changed else "yellow"
        say(f"[{colour}]{escape(result.message)}[/{colour}]")
        if result.resume_queued and result.approval is not None:
            resume.append(result.approval.session_key)
    for session_key in dict.fromkeys(resume):
        run(turn(engine, session_key))


def decide(app: App, approve: bool, words: list[str], note: str | None) -> None:
    """`pa approve` and `pa deny`: `words` are request numbers, `all`, and maybe a reason."""
    targets, trailing = split_targets(words)
    with Engine.acquire(app) as engine:
        answer_approvals(engine, approve, targets, note or trailing, asyncio.run)


def _remind_of_approvals(app: App) -> None:
    waiting = app.approvals.pending()
    if waiting:
        warn(
            f"{len(waiting)} action(s) are waiting for your answer: "
            "/approvals to see them, /approve or /deny to answer."
        )


# ---- the chat ----------------------------------------------------------------------------
def enable_history(app: App) -> None:
    """Arrow-up recalls what was typed in earlier chats (kept in the agent's home, private)."""
    try:
        import readline
    except ImportError:  # pragma: no cover - Windows
        return
    history = app.paths.home / ".chat_history"
    try:
        readline.read_history_file(history)
    except OSError:
        pass
    readline.set_history_length(1000)
    atexit.register(lambda: _save_history(readline, history))


def _save_history(readline: ModuleType, history: Path) -> None:
    try:
        readline.write_history_file(history)
        history.chmod(0o600)
    except OSError:
        pass


def chat_loop(app: App, session_key: str, read_line: Callable[[str], str] = input) -> None:
    """The interactive loop. One event loop for the whole conversation, so the HTTP connection
    pool is reused, and Ctrl-C cancels only the running turn."""
    with Engine.acquire(app) as engine:
        engine.keep_services = True  # MCP servers stay up between messages; _chat closes them
        _chat(engine, session_key, read_line)


def _chat(engine: Engine, session_key: str, read_line: Callable[[str], str]) -> None:
    app = engine.app
    where = "" if engine.inline else " · background service"
    console.print(
        Panel(
            f"[bold]{escape(app.config.agent.name)}[/bold] · model {escape(app.config.model.main)}"
            f" · session '{escape(session_key)}'{where}\n"
            "[dim]/help for commands, Ctrl-D to leave[/dim]",
            expand=False,
        )
    )
    with asyncio.Runner() as runner:
        try:
            # Anything the agent wanted to say while you were away.
            runner.run(Dispatcher(app, {"cli": CliChannel(console)}).deliver_once())
            _remind_of_approvals(app)
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
                    event_id = submit(app, line, session_key)
                    runner.run(turn(engine, session_key, event_id))
                except KeyboardInterrupt:
                    _interrupted(engine, session_key)
        finally:
            runner.run(app.stop_services())


def _interrupted(engine: Engine, session_key: str) -> None:
    if engine.inline:
        dropped = engine.app.inbox.drop_pending(session_key, "interrupted by the owner")
        say(f"[dim]interrupted ({dropped} message(s) dropped)[/dim]")
    else:
        say(
            "[dim]Stopped waiting. The background service is still working on it; "
            "the answer will be shown when you next open the chat.[/dim]"
        )


def _slash(engine: Engine, line: str, session_key: str, run: RunTurn) -> bool:
    """Handle a /command. Returns False to leave the chat."""
    app = engine.app
    words = line.split()
    command = words[0].lower()
    if command in ("/exit", "/quit"):
        return False
    if command == "/help":
        say(HELP_TEXT)
    elif command == "/new":
        app.sessions.clear(session_key)
        say("[dim]Started a fresh conversation.[/dim]")
    elif command == "/status":
        views.show_status(app)
    elif command == "/memory":
        echo(app.memory.view("/memories"))
    elif command == "/runs":
        views.show_runs(app, 8)
    elif command == "/approvals":
        views.show_approvals(app, include_decided=False)
    elif command in ("/approve", "/deny"):
        targets, note = split_targets(words[1:])
        answer_approvals(engine, command == "/approve", targets, note, run)
    else:
        say(f"[yellow]Unknown command {escape(command)}.[/yellow] Try /help.")
    return True
