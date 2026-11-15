"""`pa serve`: run the agent in the foreground, until Ctrl-C."""

from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING, Annotated

import typer

from personal_agent.channels.cli import CliChannel
from personal_agent.channels.registry import build_channels
from personal_agent.commands.common import console, fail, opened, say
from personal_agent.logs import setup_logging
from personal_agent.presence import AlreadyRunning

if TYPE_CHECKING:
    from personal_agent.daemon import Daemon

app = typer.Typer()


@app.command()
def serve(
    show_messages: Annotated[
        bool,
        typer.Option(
            "--console",
            help="Print the agent's terminal messages here (otherwise they wait for `pa chat`).",
        ),
    ] = False,
) -> None:
    """Run the agent in the foreground: timers, background events and delivery, until Ctrl-C."""
    from personal_agent.daemon import Daemon  # the agent's runtime: loaded when it is needed

    with opened() as agent:
        setup_logging(agent.paths, console=True)
        chosen = build_channels(agent, terminal=CliChannel(console) if show_messages else None)
        daemon = Daemon(
            agent,
            chosen.channels,
            services=chosen.services,
            grace_s=agent.config.runtime.shutdown_grace_s,
        )
        try:
            asyncio.run(_run(daemon))
        except AlreadyRunning as exc:
            fail(f"{exc}.", "Stop it first (or use `pa chat` to talk to it).")


async def _run(daemon: Daemon) -> None:
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        if daemon.stopping:  # a second Ctrl-C means "now"
            raise SystemExit(130)
        say("[dim]Stopping: letting running work finish (Ctrl-C again to force)...[/dim]")
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
    say(
        f"[bold]{cfg.agent.name}[/bold] is running · model {cfg.model.main} · Ctrl-C to stop. "
        "Talk to it from another terminal with `pa chat`."
    )
    listening = sorted(s.name for s in daemon.services)
    if listening:
        say(f"Also listening on: {', '.join(listening)}")
    await running
    say("[dim]Stopped.[/dim]")
