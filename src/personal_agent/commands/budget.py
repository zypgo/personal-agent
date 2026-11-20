"""`pa budget`: the spending limits and today's spend."""

from __future__ import annotations

import math
from typing import Annotated

import typer

from personal_agent.commands.common import opened, say, span, warn

app = typer.Typer(help="Spending limits and today's spend.", invoke_without_command=True)


@app.callback()
def _show(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    with opened() as agent:
        budget = agent.budget.status()
        added = f" (including ${budget.extra_today:.2f} you added)" if budget.extra_today else ""
        say(f"today      ${budget.spent_today:.4f} of ${budget.daily_limit:.2f}{added}")
        say(f"per run    ${budget.per_run_limit:.2f}")
        say(f"resets     in {span(budget.resets_in_s)} (local midnight)")
        if budget.remaining_today <= 0:
            warn("Paused: `pa budget extend 2` lets work continue today.")


@app.command("extend")
def extend(
    amount: Annotated[float, typer.Argument(help="Dollars to add to today's limit.")],
) -> None:
    """Raise today's limit and let paused work continue. Tomorrow the normal limit applies."""
    if not math.isfinite(amount) or amount <= 0:
        raise typer.BadParameter("the amount must be finite and positive")
    with opened() as agent:
        try:
            limit, released = agent.extend_budget(amount)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        say(f"Today's limit is now ${limit:.2f}; {released} waiting event(s) released.")
