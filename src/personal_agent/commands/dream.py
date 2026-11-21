"""`pa dream`: the nightly memory tidy-up, looked at and run by hand.

The daemon queues the pass by itself once a night (`[dreaming]` in config.toml). This is for
seeing what it did and for doing it now: `status` says whether it is on and what is waiting, `log`
shows the last passes and every note each one changed (every change can be undone with
`pa memory restore`), `run` does one at once, and `run --dry-run` prints what the model would read
without calling it.

`review` lists passes whose rollback would overwrite a later edit. Only the owner's explicit
`review resolve RUN_ID --keep-current` closes one; it never rewrites memory files.
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
review_app = typer.Typer(
    help="Review memory passes that could not be undone safely.", invoke_without_command=True
)
app.add_typer(review_app, name="review")


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
        if reviews := dreamer.pending_reviews():
            say(
                f"review     {len(reviews)} pass(es) need your decision; `pa dream review` lists them"
            )
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


def _show_reviews() -> None:
    from personal_agent.harness.dreaming import Dreamer

    with opened() as agent:
        reviews = Dreamer(agent).pending_reviews()
        if not reviews:
            say("[dim]No memory passes need review.[/dim]")
            return
        for review in reviews:
            say(
                f"[bold]{escape(review.run_id)}[/bold]  {format_local(review.flagged_at, agent.tz)}"
            )
            say(f"  {escape(review.reason)}")
            for change in agent.memory.changes_by_run(review.run_id):
                say(
                    f"  {escape(change['path'])}  (version {change['id']}, {escape(change['action'])})"
                )
            say("  Current files were kept; automatic rollback did not complete.")
            say("  Compare versions with `pa memory history <path>` before deciding.")
            say(f"  Keep current: pa dream review resolve {escape(review.run_id)} --keep-current")


@review_app.callback()
def review_status(ctx: typer.Context) -> None:
    """List passes awaiting an owner's decision."""
    if ctx.invoked_subcommand is None:
        _show_reviews()


@review_app.command("list")
def review_list() -> None:
    """List conflicts and the affected memory versions."""
    _show_reviews()


@review_app.command("resolve")
def review_resolve(
    run_id: str,
    keep_current: Annotated[
        bool,
        typer.Option("--keep-current", help="Accept current memory without undoing any files."),
    ] = False,
) -> None:
    """Accept the current memory and close the review."""
    from personal_agent.harness.dreaming import Dreamer

    with opened() as agent:
        try:
            resolved = Dreamer(agent).resolve_review(run_id, keep_current=keep_current)
        except ValueError as exc:
            fail(str(exc), code=2)
        if not resolved:
            fail(f"No pending review for pass {run_id}.")
        say(f"Kept current memory for {escape(run_id)} and closed its review.")
        say("Future passes will still review the unfinished input.")


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
