"""Terminal channel: prints the agent's messages."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from personal_agent.core.outbox import OutMessage


class CliChannel:
    name = "cli"

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console()

    async def send(self, message: OutMessage) -> None:
        console = self.console
        if message.kind == "notice":
            console.print(f"[dim]{escape(message.text)}[/dim]")
        else:
            prefix = "[bold red]urgent[/bold red] " if message.urgency == "urgent" else ""
            if prefix:
                console.print(prefix, end="")
            console.print(Markdown(message.text))
        console.print()
