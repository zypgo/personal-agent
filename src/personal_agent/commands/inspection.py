"""`pa status`, `pa runs`, `pa run` and `pa audit`: what the agent has been doing."""

from __future__ import annotations

from typing import Annotated, Any

import typer
from rich.markup import escape

from personal_agent.commands import views
from personal_agent.commands.common import ago, console, fail, opened, say, table

app = typer.Typer()


@app.command()
def status() -> None:
    """Queues, runs and spend."""
    with opened() as agent:
        views.show_status(agent)


@app.command()
def runs(limit: Annotated[int, typer.Option("--limit", "-n")] = 15) -> None:
    """Recent runs."""
    with opened() as agent:
        views.show_runs(agent, limit)


@app.command()
def run(run_id: Annotated[str, typer.Argument(help="A run id from `pa runs`.")]) -> None:
    """Everything recorded about one run."""
    with opened() as agent:
        record = agent.runs.get(run_id)
        if record is None:
            fail(f"No run {run_id}.")
        say(
            f"[bold]{escape(record.id)}[/bold] {record.status} · {escape(record.profile)} · "
            f"session {escape(record.session_key)}"
        )
        say(
            f"{record.requests} model calls, {record.tool_calls} tool calls, "
            f"{record.input_tokens} in ({record.cached_tokens} cached) / "
            f"{record.output_tokens} out, ${record.cost_usd:.4f}"
        )
        if record.error:
            say(f"[red]error:[/red] {escape(record.error)}")
        for row in agent.sessions.transcript(record.session_key, limit=5000):
            if row["run_id"] == run_id and row["kind"] == "item":
                kind, body = _describe(row["item"])
                say(f"[dim]{escape(kind)}[/dim] {escape(body)}")


def _describe(item: dict[str, Any]) -> tuple[str, str]:
    """What kind of thing a conversation item is, and (shortened) what it says."""
    kind = str(item.get("role") or item.get("type"))
    body = item.get("content") or item.get("arguments") or item.get("output") or item.get("name")
    if isinstance(body, list):
        body = " ".join(str(part.get("text", "")) for part in body if isinstance(part, dict))
    return kind, str(body or "")[:600]


@app.command()
def audit(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 25,
    tool: Annotated[str | None, typer.Option("--tool", help="Only this tool.")] = None,
    verdict: Annotated[str | None, typer.Option("--verdict", help="allow, ask or deny.")] = None,
    run_id: Annotated[
        str | None, typer.Option("--run", help="Only this run (from `pa runs`).")
    ] = None,
) -> None:
    """What the agent actually did: every tool call, what the policy decided and how it ended."""
    with opened() as agent:
        now = agent.clock.now()
        rows = agent.audit.recent(limit, run_id=run_id, tool=tool, verdict=verdict)
        if not rows:
            say("[dim]Nothing recorded yet.[/dim]")
            return
        shown = table("when", "tool", "decision", "how it ended", "what")
        for row in rows:
            shown.add_row(
                ago(row.ts, now),
                escape(row.tool) + (" [yellow]tainted[/yellow]" if row.tainted else ""),
                row.verdict + (f" ({row.layer})" if row.layer else ""),
                row.outcome or "-",
                escape((row.detail or row.args or "")[:70]),
            )
        console.print(shown)
