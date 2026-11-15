"""`pa dream`: the nightly memory tidy-up, looked at and run by hand.

The daemon queues the pass by itself once a night (`[dreaming]` in config.toml). This is for
seeing what it did and for doing it now: `status` says whether it is on and what is waiting, `log`
shows the last passes and every note each one changed (every change can be undone with
`pa memory restore`), `run` does one at once, and `run --dry-run` prints what the model would read
without calling it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

import typer
from rich.console import Console
from rich.markup import escape

from personal_agent.app import App
from personal_agent.clock import format_local
from personal_agent.harness.dreaming import DREAM_KEY, LAST_DAY, Dreamer

if TYPE_CHECKING:
    from personal_agent.core.runs import RunRecord


def _changes(opened: App, run: RunRecord) -> list[str]:
    return [
        f"{row['action']:<7} {row['path']}  (version {row['id']})"
        for row in opened.memory.changes_by_run(run.id)
    ]


def build(open_app: Callable[[], App], console: Console) -> typer.Typer:
    dream_app = typer.Typer(
        help="The nightly pass that tidies the agent's memory.", invoke_without_command=True
    )

    @dream_app.callback()
    def _status(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        opened = open_app()
        try:
            cfg = opened.config.dreaming
            dreamer = Dreamer(opened)
            if not cfg.enabled:
                console.print("dreaming   [dim]off[/dim] (\\[dreaming] enabled = true)")
            elif dreamer.paused():
                console.print(
                    "dreaming   [yellow]paused[/yellow] after three failed nights; "
                    "`pa dream log` says what happened, `pa dream run` tries again"
                )
            else:
                console.print(f"dreaming   on, every night after {cfg.at}")
            waiting = dreamer.new_items()
            console.print(
                f"waiting    {waiting} new item(s) in the conversation log "
                f"(a night needs {cfg.min_new_items})"
            )
            last_day = opened.db.kv_get(LAST_DAY)
            console.print(f"last night considered: {last_day or 'never'}")
            runs = opened.runs.recent(1, session_key=DREAM_KEY)
            if runs:
                run = runs[0]
                console.print(
                    f"last pass  {format_local(run.started_at, opened.tz)}, {run.status}, "
                    f"${run.cost_usd:.3f}  (`pa dream log` for details)"
                )
        finally:
            opened.close()

    @dream_app.command("log")
    def log(limit: int = typer.Option(5, "--limit", "-n", help="How many passes.")) -> None:
        """The last passes: what each said it did and which notes it changed."""
        opened = open_app()
        try:
            runs = opened.runs.recent(limit, session_key=DREAM_KEY)
            if not runs:
                console.print("[dim]No pass has run yet.[/dim]")
                return
            for run in runs:
                console.print(
                    f"[bold]{format_local(run.started_at, opened.tz)}[/bold]  {run.status}  "
                    f"${run.cost_usd:.3f}  {run.requests} model call(s)"
                )
                if run.error:
                    console.print(f"  [yellow]{escape(run.error)}[/yellow]")
                if run.final_text:
                    console.print(f"  {escape(run.final_text)}")
                changes = _changes(opened, run)
                for line in changes:
                    console.print(f"  {escape(line)}")
                if not changes and run.status in ("completed", "silent"):
                    console.print("  [dim]changed nothing[/dim]")
                console.print()
        finally:
            opened.close()

    @dream_app.command("run")
    def run(
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Print what the model would read; call nothing."
        ),
    ) -> None:
        """Do a pass now, whatever the hour and however quiet the day was."""
        opened = open_app()
        try:
            dreamer = Dreamer(opened)
            if dry_run:
                digest = dreamer.digest()
                if digest is None:
                    console.print("[dim]Nothing new to look at.[/dim]")
                    return
                console.print(escape(digest.text), highlight=False)
                console.print(
                    f"\n[dim]{digest.items} item(s), {len(digest.text)} characters; "
                    "nothing was sent anywhere.[/dim]"
                )
                return
            if opened.config.dreaming.enabled is False:
                console.print("[dim]Dreaming is off in config.toml; running one pass anyway.[/dim]")
            event_id = dreamer.enqueue(force=True)
            if event_id is None:
                console.print("[dim]Nothing new to look at.[/dim]")
                return
            from personal_agent.daemon import Engine

            engine = Engine.acquire(opened)
            try:
                with console.status("[dim]tidying memory...[/dim]"):
                    results = asyncio.run(engine.settle(DREAM_KEY, on_failure="drop"))
            finally:
                engine.close()
            for result in results:
                if result.failure is not None:
                    console.print(
                        f"[red]Error:[/red] {escape(result.failure.user_hint or result.failure.summary)}"
                    )
                    raise typer.Exit(1)
            runs = opened.runs.recent(1, session_key=DREAM_KEY)
            if runs:
                finished = runs[0]
                console.print(f"{finished.status}, ${finished.cost_usd:.3f}")
                if finished.final_text:
                    console.print(escape(finished.final_text))
                for line in _changes(opened, finished):
                    console.print(f"  {escape(line)}")
        finally:
            opened.close()

    return dream_app
