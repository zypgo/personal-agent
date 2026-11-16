"""What the terminal shows about the agent's state.

Shared by the commands (`pa status`) and by the chat's slash commands (`/status`), so the two can
never say different things.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.markup import escape

from personal_agent.clock import format_local
from personal_agent.commands.common import ago, console, say, span, table
from personal_agent.core.approvals import plain
from personal_agent.presence import daemon_state

if TYPE_CHECKING:
    from personal_agent.app import App


def show_status(app: App) -> None:
    """The agent, its queues, its runs and what it has cost."""
    now = app.clock.now()
    cfg = app.config
    say(
        f"[bold]{escape(cfg.agent.name)}[/bold] · {escape(cfg.model.main)} · "
        f"{format_local(now, app.tz)}"
    )
    state = daemon_state(app)
    if state.kind == "daemon":
        say(
            f"agent   [green]running in the background[/green] (pid {state.pid}, "
            f"heartbeat {span(state.heartbeat_age_s or 0)} ago)"
        )
    elif state.kind == "terminal":
        say(f"agent   run by a terminal session (pid {state.pid})")
    else:
        say(
            "agent   [yellow]not running[/yellow]: timers and background events wait until "
            "`pa serve` (or `pa chat`) runs"
        )
    inbox, outbox = app.inbox.counts(), app.outbox.counts()
    say(f"inbox   {_counts(inbox)}")
    say(f"outbox  {_counts(outbox)}")
    say(f"runs    {_counts(app.runs.summary())}")
    timers = app.timers.scheduled()
    if timers:
        upcoming = app.timers.next_due()
        soon = f", next in {span(upcoming - now)}" if upcoming is not None else ""
        say(f"timers  {len(timers)} active{soon}")
    waiting = app.approvals.pending()
    if waiting:
        say(f"[yellow]approvals {len(waiting)} waiting for your answer[/yellow] (`pa approvals`)")
    budget = app.budget.status()
    week = app.runs.cost_since(now - 7 * 86400) + app.budget.spend_since(now - 7 * 86400)
    say(
        f"spend   today ${budget.spent_today:.4f} of ${budget.daily_limit:.2f} · "
        f"last 7 days ${week:.4f}"
    )
    if budget.remaining_today <= 0:
        say(
            f"[yellow]Paused for the budget until local midnight (in {span(budget.resets_in_s)}). "
            "`pa budget extend 2` lets it continue today.[/yellow]"
        )
    if outbox["maybe_sent"] or outbox["failed"]:
        say("[yellow]Some messages may not have reached you: run `pa outbox`.[/yellow]")


def _counts(counts: dict[str, int]) -> str:
    shown = [f"{name} {n}" for name, n in counts.items() if n]
    return ", ".join(shown) if shown else "[dim]empty[/dim]"


def show_runs(app: App, limit: int) -> None:
    """The most recent runs, newest first."""
    now = app.clock.now()
    rows = table("id", "when", "session", "profile", "status", "calls", "tools", "cost")
    for run in app.runs.recent(limit):
        rows.add_row(
            run.id,
            ago(run.started_at, now),
            run.session_key,
            run.profile + (" (tainted)" if run.tainted else ""),
            run.status,
            str(run.requests),
            str(run.tool_calls),
            f"${run.cost_usd:.4f}",
        )
    console.print(rows)


def show_approvals(app: App, *, include_decided: bool) -> None:
    """The actions waiting for the owner's yes or no (and, if asked, the answered ones too)."""
    now = app.clock.now()
    found = app.approvals.recent(30) if include_decided else app.approvals.pending()
    if not found:
        say("[dim]Nothing is waiting for your answer.[/dim]")
        return
    rows = table("id", "status", "tool", "what", "session", "asked", "expires")
    for item in found:
        rows.add_row(
            f"#{item.id}",
            item.status,
            item.tool,
            escape(plain(item.summary)),
            item.session_key,
            ago(item.created_at, now),
            f"in {span(item.expires_at - now)}" if item.status == "pending" else "-",
        )
    console.print(rows)
