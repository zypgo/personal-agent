"""`pa timers`: the reminders and recurring jobs, whoever set them up."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.clock import format_local
from personal_agent.commands.common import console, fail, opened, say, span, table
from personal_agent.core.timers import TimerError
from personal_agent.presence import daemon_state

if TYPE_CHECKING:
    from personal_agent.app import App

app = typer.Typer(
    help="Reminders and recurring jobs the agent (or you) set up.", invoke_without_command=True
)

Ref = Annotated[str, typer.Argument(help="Timer name or id.")]
DURATION = re.compile(r"(\d+)\s*([smhd])$", re.IGNORECASE)
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> float:
    """ "90s", "45m", "2h" or "1d" as seconds."""
    match = DURATION.match(text.strip())
    if not match:
        raise typer.BadParameter("use a number and a unit, such as 90s, 45m, 2h or 1d")
    return int(match.group(1)) * UNIT_SECONDS[match.group(2).lower()]


def parse_local_time(agent: App, text: str) -> float:
    """A local time such as "2026-11-20 09:30" (in the agent's zone, unless it says its own)."""
    try:
        moment = datetime.fromisoformat(text.strip())
    except ValueError as exc:
        raise typer.BadParameter('use a local time like "2026-11-20 09:30"') from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=agent.tz)
    return moment.timestamp()


@app.callback()
def _list(
    ctx: typer.Context,
    all_: Annotated[
        bool, typer.Option("--all", "-a", help="Include finished and cancelled timers.")
    ] = False,
) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        now = agent.clock.now()
        rows = agent.timers.list(include_finished=all_)
        if not rows:
            say(
                "[dim]No timers. Ask the agent to remind you of something, or use "
                "`pa timers add`.[/dim]"
            )
            return
        shown = table("id", "name", "status", "repeats", "next", "in", "fired", "by")
        for timer in rows:
            live = timer.status in ("active", "paused")
            shown.add_row(
                str(timer.id),
                escape(timer.name),
                timer.status,
                f"{timer.cron} ({timer.tz})" if timer.cron else "once",
                format_local(timer.due_at, agent.tz) if live else "-",
                span(timer.due_at - now) if timer.status == "active" else "-",
                str(timer.fire_count),
                timer.created_by,
            )
        console.print(shown)


@app.command("add")
def add(
    name: Annotated[str, typer.Argument(help="A short unique name, such as morning-brief.")],
    prompt: Annotated[str, typer.Argument(help="What the agent should do when it fires.")],
    cron: Annotated[
        str | None, typer.Option("--cron", help='Recurring, e.g. "0 8 * * 1-5".')
    ] = None,
    at: Annotated[
        str | None, typer.Option("--at", help='Once, at a local time: "2026-11-20 09:30".')
    ] = None,
    in_: Annotated[
        str | None, typer.Option("--in", help="Once, after a delay: 90s, 45m, 2h, 1d.")
    ] = None,
    tz: Annotated[
        str | None, typer.Option("--tz", help="Time zone for --cron (default: yours).")
    ] = None,
) -> None:
    """Schedule something for the agent to do later (it tells you what it finds)."""
    if sum(option is not None for option in (cron, at, in_)) != 1:
        raise typer.BadParameter("give exactly one of --cron, --at or --in")
    with opened() as agent:
        try:
            if cron is not None:
                timer = agent.timers.add_schedule(
                    name=name, prompt=prompt, cron=cron, tz=tz, created_by="owner"
                )
            else:
                due = (
                    parse_local_time(agent, at)
                    if at is not None
                    else agent.clock.now() + parse_duration(in_ or "")
                )
                timer = agent.timers.add_followup(
                    name=name, prompt=prompt, due_at=due, created_by="owner"
                )
        except TimerError as exc:
            fail(str(exc))
        say(f"Scheduled '{escape(timer.name)}' for {format_local(timer.due_at, agent.tz)}.")
        if daemon_state(agent).kind == "stopped":
            say("[dim]It fires while the agent is running: start it with `pa serve`.[/dim]")


@app.command("cancel")
def cancel(ref: Ref) -> None:
    """Stop a timer for good."""
    _act("cancel", ref)


@app.command("pause")
def pause(ref: Ref) -> None:
    """Keep a timer but stop it firing until resumed."""
    _act("pause", ref)


@app.command("resume")
def resume(ref: Ref) -> None:
    """Un-pause a timer (a recurring one continues from its next regular time)."""
    _act("resume", ref)


DONE = {"cancel": "Cancelled", "pause": "Paused", "resume": "Resumed"}


def _act(verb: str, ref: str) -> None:
    with opened() as agent:
        do = {
            "cancel": agent.timers.cancel,
            "pause": agent.timers.pause,
            "resume": agent.timers.resume,
        }
        try:
            done = do[verb](ref)
        except TimerError as exc:  # e.g. a paused schedule that can no longer be read
            fail(str(exc))
        if not done:
            fail(f"Could not {verb} '{ref}': no such timer.")
        say(f"{DONE[verb]} '{escape(ref)}'.")
