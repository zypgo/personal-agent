"""`pa dream`: the nightly memory tidy-up, looked at and run by hand.

The daemon queues the pass by itself once a night (`[dreaming]` in config.toml). This is for
seeing what it did and for doing it now: `status` says whether it is on and what is waiting, `log`
shows the last passes and every note each one changed (every change can be undone with
`pa memory restore`), `run` does one at once, and `run --dry-run` prints what the model would read
without calling it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Annotated

import typer
from rich.markup import escape

from personal_agent.clock import format_local
from personal_agent.commands.common import console, echo, fail, opened, say

if TYPE_CHECKING:
    from personal_agent.app import App
    from personal_agent.core.runs import RunRecord

app = typer.Typer(
    help="The nightly pass that tidies the agent's memory.", invoke_without_command=True
)


def _changes(agent: App, run: RunRecord) -> list[str]:
    return [
        f"{row['action']:<7} {row['path']}  (version {row['id']})"
        for row in agent.memory.changes_by_run(run.id)
    ]


@app.callback()
def _status(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    from personal_agent.harness.dreaming import DREAM_KEY, LAST_DAY, Dreamer

    with opened() as agent:
        cfg = agent.config.dreaming
        dreamer = Dreamer(agent)
        if not cfg.enabled:
            say("dreaming   [dim]off[/dim] (\\[dreaming] enabled = true)")
        elif dreamer.paused():
            say(
                "dreaming   [yellow]paused[/yellow] after three failed nights; "
                "`pa dream log` says what happened, `pa dream run` tries again"
            )
        else:
            say(f"dreaming   on, every night after {cfg.at}")
        say(
            f"waiting    {dreamer.new_items()} new item(s) in the conversation log "
            f"(a night needs {cfg.min_new_items})"
        )
        say(f"last night considered: {agent.db.kv_get(LAST_DAY) or 'never'}")
        for run in agent.runs.recent(1, session_key=DREAM_KEY):
            say(
                f"last pass  {format_local(run.started_at, agent.tz)}, {run.status}, "
                f"${run.cost_usd:.3f}  (`pa dream log` for details)"
            )


@app.command("log")
def log(limit: Annotated[int, typer.Option("--limit", "-n", help="How many passes.")] = 5) -> None:
    """The last passes: what each said it did and which notes it changed."""
    from personal_agent.harness.dreaming import DREAM_KEY

    with opened() as agent:
        runs = agent.runs.recent(limit, session_key=DREAM_KEY)
        if not runs:
            say("[dim]No pass has run yet.[/dim]")
            return
        for run in runs:
            say(
                f"[bold]{format_local(run.started_at, agent.tz)}[/bold]  {run.status}  "
                f"${run.cost_usd:.3f}  {run.requests} model call(s)"
            )
            if run.error:
                say(f"  [yellow]{escape(run.error)}[/yellow]")
            if run.final_text:
                say(f"  {escape(run.final_text)}")
            changes = _changes(agent, run)
            for line in changes:
                say(f"  {escape(line)}")
            if not changes and run.status in ("completed", "silent"):
                say("  [dim]changed nothing[/dim]")
            say()


@app.command("run")
def run(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print what the model would read; call nothing.")
    ] = False,
) -> None:
    """Do a pass now, whatever the hour and however quiet the day was."""
    from personal_agent.daemon import Engine
    from personal_agent.harness.dreaming import DREAM_KEY, Dreamer

    with opened() as agent:
        dreamer = Dreamer(agent)
        if dry_run:
            digest = dreamer.digest()
            if digest is None:
                say("[dim]Nothing new to look at.[/dim]")
                return
            echo(digest.text)
            say(
                f"\n[dim]{digest.items} item(s), {len(digest.text)} characters; "
                "nothing was sent anywhere.[/dim]"
            )
            return
        if agent.config.dreaming.enabled is False:
            say("[dim]Dreaming is off in config.toml; running one pass anyway.[/dim]")
        if dreamer.enqueue(force=True) is None:
            say("[dim]Nothing new to look at.[/dim]")
            return
        with Engine.acquire(agent) as engine, console.status("[dim]tidying memory...[/dim]"):
            results = asyncio.run(engine.settle(DREAM_KEY, on_failure="drop"))
        for result in results:
            if result.failure is not None:
                fail("Error:", result.failure.user_hint or result.failure.summary)
        for finished in agent.runs.recent(1, session_key=DREAM_KEY):
            say(f"{finished.status}, ${finished.cost_usd:.3f}")
            if finished.final_text:
                say(escape(finished.final_text))
            for line in _changes(agent, finished):
                say(f"  {escape(line)}")
