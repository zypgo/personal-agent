"""Terminal channel: prints the agent's messages."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape

from personal_agent.core.outbox import OutMessage
from personal_agent.security.sanitize import strip_invisible
from personal_agent.terminal import SafeConsole


class CliChannel:
    name = "cli"

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or SafeConsole()

    async def send(self, message: OutMessage) -> None:
        console = self.console
        # Cleaned here as well as by the console: a console someone passes in need not be a safe one.
        text = strip_invisible(message.text)
        if message.kind == "notice":
            console.print(f"[dim]{escape(text)}[/dim]")
        else:
            prefix = "[bold red]urgent[/bold red] " if message.urgency == "urgent" else ""
            if prefix:
                console.print(prefix, end="")
            console.print(Markdown(text))
        console.print()
